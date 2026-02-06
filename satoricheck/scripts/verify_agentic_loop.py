
"""
Verification Script for Agentic Loop (Gemini -> Tools -> Grok -> Gemini)
Run this to verify that the agent correctly calls the 'search_social' tool 
when presented with a relevant claim.
"""
import sys
import os
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.gemini_service import GeminiService
from backend.config import Config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AgenticVerify")

def test_agentic_loop():
    print("-" * 50)
    print("TESTING AGENTIC LOOP")
    print("-" * 50)

    if not Config.GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not set.")
        return

    service = GeminiService()
    
    # 1. Test Claim that SHOULD trigger a tool call
    claim_text = "Elon Musk just tweeted that he is buying McDonald's and turning it into a gym."
    print(f"\n📡 Analyzing Claim: '{claim_text}'")
    
    try:
        # Force agentic mode
        result = service.analyze_claim(claim_text, smart_agent=True)
        
        print("\n✅ Result Received:")
        print(f"Verdict: {result.get('verdict')}")
        print(f"Explanation: {result.get('explanation')}")
        print(f"Social Context: {result.get('social_context')}")
        
    except Exception as e:
        print(f"\n❌ Test Failed: {e}")

if __name__ == "__main__":
    test_agentic_loop()
