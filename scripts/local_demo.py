"""
Local demo — run the review pipeline directly in VS Code with NO GitHub
webhook, no ngrok, no repo setup. Only requires ANTHROPIC_API_KEY.

This calls the exact same Detect -> Explain -> Fix -> Judge pipeline used
by the real webhook server (app/main.py), just fed with a hardcoded sample
diff instead of a live PR.

Usage:
    python scripts/local_demo.py
"""
import os
import sys

# Make sure the project root (the folder containing "app/") is importable
# regardless of how this script is invoked, e.g. `python scripts\local_demo.py`
# from any working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import require_config
from app.agents import run_pipeline

SAMPLE_FILENAME = "app/utils/payments.py"

SAMPLE_FILE_CONTENT = '''
def charge_card(card_token, amount_cents):
    # TODO: handle currency conversion
    response = payment_gateway.charge(card_token, amount_cents)
    return response

def refund(charge_id, amount_cents):
    response = payment_gateway.refund(charge_id, amount_cents)
    return response
'''

SAMPLE_PATCH = '''@@ -1,7 +1,9 @@
 def charge_card(card_token, amount_cents):
     # TODO: handle currency conversion
-    response = payment_gateway.charge(card_token, amount_cents)
+    response = payment_gateway.charge(card_token, amount_cents)
+    if amount_cents < 0:
+        amount_cents = abs(amount_cents)
     return response
 
 def refund(charge_id, amount_cents):
'''

REPO_FULL_NAME = "demo-org/demo-repo"


def main():
    require_config()
    print(f"Running review pipeline on sample diff for {SAMPLE_FILENAME} ...\n")

    issues = run_pipeline(SAMPLE_FILENAME, SAMPLE_PATCH, SAMPLE_FILE_CONTENT, REPO_FULL_NAME)

    if not issues:
        print("No issues found.")
        return

    for i, issue in enumerate(issues, start=1):
        print(f"--- Issue {i} ---")
        print(f"File:       {issue.file}:{issue.line}")
        print(f"Severity:   {issue.severity}")
        print(f"Category:   {issue.category}")
        print(f"Summary:    {issue.summary}")
        print(f"Explanation:\n  {issue.explanation}")
        if issue.auto_fixable:
            print(f"Confidence: {issue.confidence:.2f}")
            print(f"Proposed fix:\n{issue.fix_diff}")
        print()


if __name__ == "__main__":
    main()
