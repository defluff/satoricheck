
"""
Verification Script for Intelligent Batch Triage.
"""
import sys
import os
import logging
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.gemini_service import GeminiService
from backend.config import Config

# Configure logging to see Triage info
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BatchVerify")

def test_batch_triage():
    print("-" * 50)
    print("TESTING BATCH TRIAGE ARCHITECTURE")
    print("-" * 50)

    if not Config.GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not set.")
        return

    service = GeminiService()
    
    # Mix of claims:
    # 1. Static (Historical/Scientific)
    # 2. Dynamic (breaking news/social)
    claims = [
        "Water boils at 100 degrees Celsius at sea level.",             # Static
        "Elon Musk just tweeted that he is buying McDonald's.",         # Dynamic (Viral/Social)
        "The capital of France is Paris.",                              # Static
        "Breaking news: Aliens landed in Times Square 5 minutes ago."   # Dynamic (Breaking)
    ]
    
    print(f"\n📡 Analyzing {len(claims)} mixed claims...")
    
    try:
        results = service.analyze_claims_batch(claims)
        
        print("\n✅ Batch Analysis Complete!")
        print("-" * 30)
        
        for i, res in enumerate(results):
            claim = claims[i]
            verdict = res.get('verdict')
            explanation = res.get('explanation', '')[:100] + "..."
            
            print(f"Claim #{i+1}: {claim[:40]}...")
            print(f"Verdict: {verdict}")
            print(f"Explanation: {explanation}")
            print("-" * 30)
            
            # Heuristic check for success
            if i == 1 and "Elon" in claim:
                # Should detect it's fake/satire if Agentic worked
                if "FALSE" in str(verdict) or "Fabricated" in explanation:
                    print("   -> 🌟 Agentic Logic likely worked (Satire detected)")
        
    except Exception as e:
        print(f"\n❌ Batch Test Failed: {e}")

if __name__ == "__main__":
    test_batch_triage()
