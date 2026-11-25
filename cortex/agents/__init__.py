"""The AI side of Cortex: every agent and the loop they all share.

    loop.py         run_tool_agent(): the one place Claude runs as an agent
    claude.py       how Cortex connects to Claude (login, options)
    tools.py        search_code (the one custom tool) + the per-run record
    chat.py         chat agent: answers a question with cited sources
    tour.py         tour agent: designs a guided reading tour
    walkthrough.py  planner + writer agents: a long-form project walkthrough
"""
