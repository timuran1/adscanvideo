import os
import json
import sys
from faster_whisper import WhisperModel
model_name = os.getenv('ADSCAN_WHISPER_MODEL', 'base')
model = WhisperModel(model_name, device='cpu', compute_type='int8', cpu_threads=1, num_workers=1)
segments, _ = model.transcribe(sys.argv[1], vad_filter=True)
print(json.dumps([{'start': s.start, 'end': s.end, 'text': s.text.strip()} for s in segments if s.text.strip()]))
