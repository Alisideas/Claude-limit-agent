# Claude Limit Agent

Automatically resume a Claude Code terminal session after a usage limit resets.

Claude Limit Agent is a tiny local terminal wrapper. You start Claude through this
script, and it watches the terminal output for limit/reset messages like:

```text
You've hit your limit · resets 3:40am (Europe/Istanbul)
```

When it detects the reset time, it waits until that time and sends a continuation
prompt back into the same Claude session.

## Why This Exists

Claude Code sessions sometimes stop because of usage limits. The terminal usually
shows when the session will reset, but you still have to remember to come back and
continue the work manually.

This tool handles that waiting step for you.

## Features

- Runs Claude Code inside a watched terminal session
- Detects common reset messages and times
- Supports local clock times, relative waits, and ISO timestamps
- Sends a configurable continuation prompt when the limit resets
- Works locally with no server, account sharing, or background service
- Includes a dry-run mode for testing detection

## Requirements

- macOS or Linux
- Python 3.9+
- Claude Code CLI installed and available as `claude`

Check that Claude works first:

```bash
claude --version
```

## Installation

Clone the repository:

```bash
git clone https://github.com/Alisideas/Claude-limit-agent.git
cd Claude-limit-agent
```

Make the script executable:

```bash
chmod +x claude_limit_agent.py
```

Run it:

```bash
./claude_limit_agent.py
```

That is it. There are no Python packages to install.

## Usage

### Start Claude With The Agent

```bash
./claude_limit_agent.py
```

This starts:

```bash
claude
```

inside the watched terminal session.

### Use A Custom Claude Command

If you normally start Claude with extra flags, pass the command in quotes:

```bash
./claude_limit_agent.py --command "claude --continue"
```

or:

```bash
./claude_limit_agent.py --command "claude /path/to/project"
```

### Change The Resume Prompt

By default, the agent sends:

```text
continue, lets do the rest ...
```

You can customize it:

```bash
./claude_limit_agent.py --resume-text "continue from where we stopped"
```

### Test Without Sending Anything

Use dry-run mode if you want to confirm that detection works without sending the
resume prompt into Claude:

```bash
./claude_limit_agent.py --dry-run
```

### Hide Agent Logs

```bash
./claude_limit_agent.py --quiet
```

## Example

Start the agent:

```bash
./claude_limit_agent.py
```

If Claude prints something like:

```text
You've hit your limit · resets 3:40am (Europe/Istanbul)
```

the agent logs:

```text
[claude-limit-agent] Detected limit. Will resume at 2026-05-03 03:40:00 +03
```

Leave the terminal open. At the reset time, the agent sends the configured resume
prompt into the Claude session automatically.

## Supported Reset Formats

The parser recognizes common messages such as:

```text
try again at 4:30 PM
come back tomorrow at 09:00
resets in 2 hours 15 minutes
available at 2026-05-03T14:30:00+03:00
resets 3:40am (Europe/Istanbul)
```

If Claude changes the wording of its limit messages, update the patterns in
`LimitParser`.

## How It Works

This tool uses a pseudo-terminal. It starts Claude as a child process, then relays:

- your keyboard input to Claude
- Claude's output back to your terminal
- the configured resume prompt when a reset time is detected

It does not scrape your screen, control macOS Terminal, or attach to an existing
Claude session. You need to start Claude through this wrapper.

## Important Notes

- Keep the terminal window open while waiting.
- If you close the agent, the scheduled resume is canceled.
- This tool only resumes the local Claude CLI process it started.
- It does not bypass Claude limits. It only waits until the reset time Claude gives
  you.
- It runs entirely on your machine.

## Troubleshooting

### `Command not found: claude`

Claude Code is not installed, or it is not available in your `PATH`.

Try:

```bash
which claude
```

If that prints nothing, install Claude Code or fix your shell path.

### `Permission denied`

Make the script executable:

```bash
chmod +x claude_limit_agent.py
```

Then run it again:

```bash
./claude_limit_agent.py
```

### The Agent Did Not Detect The Limit

Run it again with visible logs:

```bash
./claude_limit_agent.py
```

Then copy the exact Claude limit message and add a matching pattern in
`LimitParser`.

### Can It Watch An Already Open Claude Terminal?

No. The agent needs to start Claude itself so it can safely read output and send
input through the same pseudo-terminal.

## Development

Run a syntax check:

```bash
python3 -m py_compile claude_limit_agent.py
```

Run a quick parser check:

```bash
python3 -c "import datetime as dt; from claude_limit_agent import LimitParser; now=dt.datetime.now().astimezone(); print(LimitParser().parse('resets in 2 hours 15 minutes', now))"
```

## License

MIT
