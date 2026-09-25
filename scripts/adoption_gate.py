"""AD-26 adoption gate: one exit status for the agent path."""
import subprocess
import sys

COMMANDS = [
    ["node", "--test", "agent/*.test.mjs", "tests/markdown.test.mjs"],
    [sys.executable, "-m", "pytest", "-q",
     "tests/test_wiki_agent_contracts.py", "tests/test_wiki_agent_domain.py",
     "tests/test_wiki_agent_application.py", "tests/test_wiki_agent_binding.py",
     "tests/test_wiki_agent_adapter.py", "tests/test_wiki_agent_bootstrap.py",
     "tests/test_wiki_agent_tokenizer.py",
     "--ignore-glob=*_browser.py"],
    [sys.executable, "-m", "pytest", "-q", "tests", "--ignore-glob=*_browser.py",
     "--ignore=tests/test_story_1_11_docker.py"],
]
for command in COMMANDS:
    if subprocess.run(command, check=False).returncode != 0:
        sys.exit(1)
