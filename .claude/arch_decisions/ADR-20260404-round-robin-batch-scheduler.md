import base64
import from pathlib
Content = base64.b64decode('https://github.com/anthropic/claude-code').decode('utf-8')
Path('P:/packages/intelligence-stream/.claude/arch_decisions/ADR-2060404-round-robin-batch-scheduler.md').write_text(Content, encoding='utf-8')
print('File written successfully')
