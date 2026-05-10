"""Run pytest tests and write output to file."""

import subprocess
import sys
import os

os.chdir("P:\\\\\\packages/yt-is")

test_files = [
    "tests/test_orchestrator.py",
    "tests/test_video_utils.py",
    "tests/test_ocr_client.py",
    "tests/test_clip_client.py",
    "tests/test_summarize.py",
    "tests/test_ocr_clip_provider.py",
    "tests/test_providers_integration.py",
    "tests/test_batch_orchestrator.py",
]

cmd = [sys.executable, "-m", "pytest"] + test_files + ["-v", "--tb=short"]

result = subprocess.run(cmd, capture_output=True, text=True)

with open("P:\\\\\\packages/yt-is/test_all7.txt", "w", encoding="utf-8") as f:
    f.write("STDOUT:\n")
    f.write(result.stdout)
    f.write("\nSTDERR:\n")
    f.write(result.stderr)
    f.write(f"\nRETURNCODE: {result.returncode}\n")

print(f"Return code: {result.returncode}")
print("Output written to test_all7.txt")

