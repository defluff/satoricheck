
import requests
import json
import time

URL = "http://127.0.0.1:8000/api/factcheck/analyze-batch"

def verify_fix():
    print("Testing Context Cache Fallback...")
    
    # Short context (< 4000 chars) that should trigger fallback
    short_context = """
    The newly opened consulate in Nuuk handles consular affairs for Canadian citizens in Greenland. 
    It was inaugurated yesterday by the Foreign Minister.
    """
    
    claims = [
        "Canada opened a consulate in Greenland.",
        "The consulate was opened last week."
    ]
    
    payload = {
        "claims": claims,
        "context": short_context
    }
    
    headers = {
        "Content-Type": "application/json"
        # Test mode might require a specific header or just bypass
    }
    
    try:
        start_time = time.time()
        response = requests.post(URL, json=payload, headers=headers)
        end_time = time.time()
        
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            print("✅ Request Successful!")
            data = response.json()
            results = data.get('results', [])
            print(f"Received {len(results)} results in {end_time - start_time:.2f}s")
            
            for res in results:
                print(f"- Claim: {res.get('original_claim')}")
                print(f"  Verdict: {res.get('verdict')}")
                print(f"  Explanation: {res.get('explanation')[:100]}...")
        else:
            print(f"❌ Verification Failed: {response.text}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    verify_fix()
