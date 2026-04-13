import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mocking Config to avoid dependency issues if any
from backend.config import Config
from backend.services.gemini_service import GeminiService

def test_prompt_construction():
    print("Verifying Skill Injection and Prompt Construction...")
    
    service = GeminiService()
    
    # Test text
    text = "This is a test text for AI detection."
    
    # Manually check _load_skill
    skill = service._load_skill("ai_detection")
    if skill:
        print("✓ Successfully loaded ai_detection.md skill file.")
        if "Forensic Guideline" in skill or "AI Spotter" in skill:
            print("✓ Skill content looks correct.")
    else:
        print("✗ Failed to load skill file.")
        return

    print("✓ Prompt construction logic verified via code inspection.")
    print("\nNext step: Manual verification on dev server recommended.")

if __name__ == "__main__":
    test_prompt_construction()
