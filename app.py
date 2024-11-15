# model_service.py
from flask import Flask, jsonify, request
import torch
from transformers import BertTokenizer, BertForSequenceClassification
import time
import logging
from multiprocessing import Process, Queue, Value, Lock
import os
import ctypes
import numpy as np
import psutil
# from waitress import serve

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ModelWorker:
    def __init__(self, model_path, request_queue, response_queue, worker_id):
        self.model_path = model_path
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.worker_id = worker_id
        
        # Pin to specific CPU core on Unix
        os.sched_setaffinity(0, {worker_id})
        torch.set_num_threads(1)  # Use single thread per worker
        
        self.load_model()
        
    def load_model(self):
        logger.info(f"Worker {self.worker_id}: Loading model")
        
        self.tokenizer = BertTokenizer.from_pretrained(
            'google-bert/bert-base-multilingual-cased',
            do_lower_case=False,
            strip_accents=False
        )
        
        self.model = BertForSequenceClassification.from_pretrained(self.model_path)
        self.model.eval()
        
        # JIT compile model for better performance
        # dummy_input = self.tokenizer(
        #     "dummy text",
        #     return_tensors="pt",
        #     padding=True,
        #     truncation=True,
        #     max_length=64
        # )
        
        # with torch.no_grad():
        #     self.model = torch.jit.trace(
        #         self.model,
        #         # {
        #             dummy_input
        #             # dummy_input['input_ids'],
        #             # dummy_input['attention_mask'],
        #             # dummy_input['token_type_ids']
        #     # )
        #     )
        #     self.model = torch.jit.optimize_for_inference(self.model)
            
        logger.info(f"Worker {self.worker_id}: Model loaded successfully")
        
    def predict(self, sentences, request_ids):
        if isinstance(sentences, str):
            sentences = [sentences]
            request_ids = [request_ids]
            
        # Tokenize
        inputs = self.tokenizer(
            sentences,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=64
        )
        
        # Inference
        with torch.no_grad():
            outputs = self.model(
                inputs['input_ids'],
                inputs['attention_mask'],
                inputs['token_type_ids']
            )
            predictions = torch.softmax(outputs.logits, dim=1)
            
        # Process results
        results = []
        for i, pred in enumerate(predictions):
            predicted_class = torch.argmax(pred).item()
            confidence = pred[predicted_class].item()
            results.append({
                'request_id': request_ids[i],
                'class': predicted_class,
                'confidence': confidence,
                'worker_id': self.worker_id
            })
            
        return results

    def run(self):
        logger.info(f"Worker {self.worker_id}: Starting prediction loop")
        
        batch = []
        batch_ids = []
        last_process_time = time.time()
        
        while True:
            try:
                # Get request from queue with timeout
                try:
                    request = self.request_queue.get(timeout=0.01)
                    batch.append(request['sentence'])
                    batch_ids.append(request['request_id'])
                except:
                    pass
                
                # Process batch if it's full or timeout reached
                current_time = time.time()
                if len(batch) >= 32 or (batch and current_time - last_process_time > 0.1):
                    if batch:
                        results = self.predict(batch, batch_ids)
                        for result in results:
                            self.response_queue.put(result)
                            
                        batch = []
                        batch_ids = []
                        last_process_time = current_time
                        
            except Exception as e:
                logger.error(f"Worker {self.worker_id}: Error processing batch: {str(e)}")
                for request_id in batch_ids:
                    self.response_queue.put({
                        'request_id': request_id,
                        'error': str(e),
                        'worker_id': self.worker_id
                    })
                batch = []
                batch_ids = []

def start_worker(model_path, request_queue, response_queue, worker_id):
    worker = ModelWorker(model_path, request_queue, response_queue, worker_id)
    worker.run()

# Flask application
app = Flask(__name__)

# Shared queues and worker counter
request_queue = Queue()
response_queue = Queue()
worker_counter = Value(ctypes.c_int, 0)
counter_lock = Lock()

def get_next_worker():
    """Round-robin worker selection"""
    with counter_lock:
        worker_counter.value = (worker_counter.value + 1) % 4
        return worker_counter.value

@app.route('/bert/classify/<string:sentence>', methods=['GET'])
def classify(sentence):
    try:
        start_time = time.time()
        request_id = int(time.time() * 1000000)  # Microsecond timestamp as ID
        
        # Create request
        request_data = {
            'request_id': request_id,
            'sentence': sentence
        }
        
        # Send to worker queue
        request_queue.put(request_data)
        
        # Wait for result with timeout
        max_wait = 30  # seconds
        while time.time() - start_time < max_wait:
            try:
                result = response_queue.get(timeout=0.1)
                if result['request_id'] == request_id:
                    if 'error' in result:
                        return jsonify({'error': result['error']}), 500
                    
                    # Map prediction to class name
                    class_mapping = {0: 'none', 1: 'product', 2: 'series'}
                    predicted_label = class_mapping.get(result['class'], 'none')
                    
                    return jsonify({
                        'class': predicted_label,
                        'sentence': sentence,
                        'confidence': result['confidence'],
                        'processing_time': round(time.time() - start_time, 4),
                        'worker_id': result['worker_id']
                    })
                else:
                    # Put back result if it's not ours
                    response_queue.put(result)
            except:
                continue
                
        return jsonify({'error': 'Request timeout'}), 408
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return jsonify({'error': str(e)}), 500

def start_server():
    # Start worker processes
    num_workers = 2
    workers = []
    
    for i in range(num_workers):
        p = Process(
            target=start_worker,
            args=('./multi_base', request_queue, response_queue, i)
        )
        p.daemon = True
        p.start()
        workers.append(p)
        
    # Start Flask server
    # serve(app, host='0.0.0.0', port=5000, threads=16, backlog=2048)
    app.run()
    
    # Clean up workers
    for p in workers:
        p.terminate()
        p.join()

if __name__ == '__main__':
    start_server()
