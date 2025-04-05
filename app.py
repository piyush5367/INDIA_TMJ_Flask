from flask import Flask, request, jsonify
import re
import pdfplumber
import logging
from io import BytesIO
import gc
import os
import uuid
import time
from threading import Event
from datetime import datetime, timedelta

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Enable CORS
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST')
    return response

# Global dictionary to store processing status and cancellation flags
processing_tasks = {}

def cleanup_old_tasks():
    """Remove tasks older than 1 hour"""
    now = datetime.now()
    old_tasks = [task_id for task_id, task in processing_tasks.items() 
                if now - task.get('created_at', now) > timedelta(hours=1)]
    for task_id in old_tasks:
        del processing_tasks[task_id]

class TMJNumberExtractor:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.cancel_flag = processing_tasks[task_id]['cancel_flag']
        self.section_order = ['advertisement', 'corrigenda', 'rc', 'renewal', 'pr_section']
        
        self.section_markers = {
            'corrigenda': re.compile(r'CORRIGENDA', re.IGNORECASE),
            'renewal': re.compile(r'FOLLOWING TRADE MARKS REGISTRATION RENEWED', re.IGNORECASE),
            'pr_section': re.compile(r'PR SECTION', re.IGNORECASE),
            'advertisement': re.compile(r'TRADE MARKS JOURNAL', re.IGNORECASE)
        }
        
        self.patterns = {
            'advertisement': re.compile(r'(?<!\d)(\d{5,})(?:\s+\d{2}/\d{2}/\d{4})?'),
            'corrigenda': re.compile(r'(?<!\d)(\d{5,})(?!\d)'),
            'rc': re.compile(r'\b(\d{5,})\b'),
            'renewal': [
                re.compile(r'(?<!\d)(\d{5,})(?!\d)'),
                re.compile(r'Application No\s*:?\s*(\d{5,})')
            ],
            'pr_section': re.compile(r'(\d{5,})\s*[-–]')
        }
        
        self.logger = logging.getLogger(__name__)

    def _remove_duplicates(self, numbers):
        """Remove duplicate numbers while preserving order"""
        return list(dict.fromkeys(numbers))

    def extract_section_numbers(self, text, section):
        if self.cancel_flag.is_set() or not text:
            return []
        
        numbers = []
        lines = text.split('\n')
        
        try:
            if section == 'advertisement':
                for line in lines:
                    if self.cancel_flag.is_set():
                        return []
                    matches = self.patterns['advertisement'].findall(line.strip())
                    numbers.extend(matches)
            
            elif section == 'corrigenda':
                in_section = False
                for line in lines:
                    if self.cancel_flag.is_set():
                        return []
                    if self.section_markers['corrigenda'].search(line):
                        in_section = True
                        continue
                    if self.section_markers['renewal'].search(line):
                        break
                    if in_section:
                        matches = self.patterns['corrigenda'].findall(line.strip())
                        numbers.extend(matches)
            
            elif section == 'rc':
                for line in lines:
                    if self.cancel_flag.is_set():
                        return []
                    if len(cols := line.split()) == 5 and all(c.isdigit() for c in cols):
                        numbers.extend(cols)
            
            elif section == 'renewal':
                in_section = False
                for line in lines:
                    if self.cancel_flag.is_set():
                        return []
                    if self.section_markers['renewal'].search(line):
                        in_section = True
                        continue
                    if in_section:
                        for pattern in self.patterns['renewal']:
                            numbers.extend(pattern.findall(line.strip()))
            
            elif section == 'pr_section':
                in_section = False
                for line in lines:
                    if self.cancel_flag.is_set():
                        return []
                    if self.section_markers['pr_section'].search(line):
                        in_section = True
                        continue
                    if in_section:
                        matches = self.patterns['pr_section'].findall(line.strip())
                        numbers.extend(matches)
            
        except Exception as e:
            self.logger.error(f"Error extracting {section}: {str(e)}")
            return []
        
        return self._remove_duplicates(numbers)

    def process_pdf(self, pdf_file):
        results = {section: [] for section in self.section_order}
        
        try:
            with pdfplumber.open(pdf_file.stream) as pdf:
                total_pages = len(pdf.pages)
                processing_tasks[self.task_id]['total_pages'] = total_pages * len(self.section_order)
                processing_tasks[self.task_id]['processed_pages'] = 0
                processing_tasks[self.task_id]['progress'] = 0
                
                for section in self.section_order:
                    if self.cancel_flag.is_set():
                        break
                    processing_tasks[self.task_id]['current_section'] = section
                    
                    for page_index, page in enumerate(pdf.pages):
                        if self.cancel_flag.is_set():
                            break
                        
                        processing_tasks[self.task_id]['processed_pages'] += 1
                        current_progress = (processing_tasks[self.task_id]['processed_pages'] / 
                                          processing_tasks[self.task_id]['total_pages']) * 100
                        processing_tasks[self.task_id]['progress'] = current_progress
                        
                        try:
                            text = page.extract_text() or ""
                            numbers = self.extract_section_numbers(text, section)
                            results[section].extend(numbers)
                        except Exception as e:
                            self.logger.error(f"Page {page_index} error: {str(e)}")
                            continue
                
                return {k: self._remove_duplicates(v) for k, v in results.items()}
        
        except Exception as e:
            self.logger.error(f"PDF processing failed: {str(e)}")
            return {"error": f"Error processing PDF: {str(e)}"}
        finally:
            gc.collect()

@app.route('/upload', methods=['POST'])
def upload_pdf():
    cleanup_old_tasks()
    
    if 'pdf_file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    pdf_file = request.files['pdf_file']
    
    if not pdf_file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Only PDF files are allowed"}), 400
    
    pdf_file.stream.seek(0, 2)
    file_size = pdf_file.stream.tell()
    pdf_file.stream.seek(0)
    if file_size > 50 * 1024 * 1024:
        return jsonify({"error": "File size exceeds 50MB limit"}), 400
    
    task_id = str(uuid.uuid4())
    processing_tasks[task_id] = {
        "cancel_flag": Event(),
        "progress": 0,
        "created_at": datetime.now(),
        "status": "processing"
    }

    extractor = TMJNumberExtractor(task_id)
    result = extractor.process_pdf(pdf_file)
    
    if processing_tasks[task_id]['cancel_flag'].is_set():
        result = {"error": "Processing cancelled by user"}
    
    result['task_id'] = task_id
    processing_tasks[task_id]['status'] = 'completed'
    processing_tasks[task_id]['completed_at'] = datetime.now()
    
    return jsonify(result)

@app.route('/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    cleanup_old_tasks()
    
    if task_id not in processing_tasks:
        return jsonify({"error": "Invalid task ID"}), 404
    
    task = processing_tasks[task_id]
    return jsonify({
        "progress": task.get('progress', 0),
        "current_section": task.get('current_section', ''),
        "status": "cancelled" if task['cancel_flag'].is_set() else task.get('status', 'processing'),
        "task_id": task_id
    })

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    cleanup_old_tasks()
    
    if task_id not in processing_tasks:
        return jsonify({"error": "Invalid task ID"}), 404
    
    processing_tasks[task_id]['cancel_flag'].set()
    processing_tasks[task_id]['status'] = 'cancelled'
    return jsonify({
        "status": "cancellation_requested",
        "task_id": task_id
    })

@app.route('/')
def index():
    return "PDF Number Extractor API is running"

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)