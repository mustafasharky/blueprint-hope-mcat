# ChatGPT connection setup

Run `run_blueprint.cmd` or `python blueprint_run.py` in a terminal. Before starting
the browser workflow, the script checks for an OpenAI API key in this order:

1. A nonempty `OPENAI_API_KEY` environment variable, if you already configured one.
2. This tool's saved key in your operating system's credential store.
3. A terminal prompt with hidden input. Paste your key and press Enter; it is saved
   in the OS credential store and reused automatically on subsequent runs.

Create your own key at [OpenAI API keys](https://platform.openai.com/api-keys).
Every person running this project uses their own key. Do not paste a key into a
chat, source file, command-line argument, or Git commit. The prompt is application
input, so the key does not enter your shell's command history.

This configures credentials only. It does not test the key with OpenAI, make a
billable API request, upload study data, or connect to a ChatGPT web conversation.
Native ChatGPT delivery is not implemented by the API key. The optional companion
now provides MCP tools and explicit local API item authoring; see the
[companion guide](COMPANION.md) for setup, budgets and live capability gates.
The browser workflow still needs
the report plan described in the [README](../README.md).

## Local storage

Install `requirements-browser.txt` with the Python interpreter used to run the
script. Its `keyring` dependency provides native credential-store access:

| Platform | Store |
|---|---|
| Windows | Windows Credential Manager, explicitly using local-machine persistence instead of roaming persistence |
| macOS | macOS Keychain, using a nonsynchronizing generic password item |
| Linux | A local Freedesktop Secret Service, such as GNOME Keyring, with an available, unlocked session |

The credential is identified by service `blueprint-hope-openai` and account
`api-key`. It belongs to the current OS user and is shared by that user's local
copies of this project. Windows is the supported platform for the automatic
browser handoff; credential setup itself also supports macOS and Linux.

The script selects these native backends directly; it does not use third-party
keyring plugins, plaintext fallback files, or a hosted credential service. The
key is never written to this repository, `private/`, report exports, HTML,
progress JSON, or logs. Environment-provided keys are used without saving them,
and prompted keys are not copied into the environment passed to browser processes.
The OS store protects the key at rest; programs running as the same OS user may
still be able to access it. Protect your OS account and its backups.

This follows OpenAI's guidance to keep API keys secret and load them from
environment variables or a credential service:
[OpenAI authentication guidance](https://developers.openai.com/api/reference/overview#authentication).
The credential backend comes from the open-source
[Python keyring library](https://keyring.readthedocs.io/en/latest/).

## Replace or remove the key

Use the same interpreter as the launcher. These commands exit after managing the
key and do not open Blueprint or require a report plan:

```text
.venv\Scripts\python.exe blueprint_run.py --configure-openai
.venv\Scripts\python.exe blueprint_run.py --forget-openai
```

`--configure-openai` always prompts and saves a new key, replacing the saved value.
An existing `OPENAI_API_KEY` still takes precedence on normal runs; unset or update
it if you want to use the replacement. `--forget-openai` removes only this tool's
saved credential. It does not change environment variables or revoke the key at
OpenAI. Revoke an old key through the OpenAI dashboard when appropriate.

For report capture without credential setup:

```text
.venv\Scripts\python.exe blueprint_run.py --local-only
```

Unattended runs can reuse a key accessible to the same OS user or receive
`OPENAI_API_KEY` through their environment. If no key is available, the script
exits with instructions instead of reading a secret from piped input. If the OS
store is locked or unavailable, unlock it, use an environment-provided key, or
run with `--local-only`. It never silently downgrades to plaintext storage.
Blank or malformed input is rejected; Ctrl+C cancels setup. If the terminal cannot
hide input, setup stops before accepting a key.
