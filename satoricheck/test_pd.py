import sys
import os
import base64
import __main__
sys.path.append(os.path.dirname(os.path.abspath(__main__.__file__)))

from backend.services.pitchdeck_service import pitchdeck_service
pdf_bytes = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\nxref\n0 3\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \ntrailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n111\n%%EOF'

try:
    pitchdeck_service.analyze_pitch_deck(pdf_bytes)
    print("Success")
except Exception as e:
    import traceback
    traceback.print_exc()
