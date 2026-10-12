# Context-Mode Tool Routing Directives

To maximize token efficiency and prevent blowing past context limits, NEVER execute raw terminal reads or output large raw file contents into chat. ALWAYS delegate search, file, terminal, and code analysis operations to the attached `context-mode` MCP tools based on the following classification rules:

## 1. Code Search & Discovery
* **`ctx_search`**: Use when performing keyword, symbol, pattern, or regular expression searches across the workspace (e.g., finding FastAPI router definitions, class names, imports, or config keys).
* **`ctx_index`**: Use to build or refresh the workspace repository index (SQLite FTS5 database) before running deep structural queries on a fresh or heavily modified codebase.

## 2. File Reading & Inspection
* **`ctx_execute_file`**: Use when inspecting individual source code files or configuration files. Captures and compresses output, returning indexed summaries rather than raw text.
* **`ctx_batch_execute`**: Use when multiple files or commands need to be processed simultaneously in a single turn to minimize multi-step tool latency.

## 3. Terminal & Shell Command Execution
* **`ctx_execute`**: Use for running shell commands, linters, tests, or Python scripts where the stdout/stderr might generate high volume logs.
* **`ctx_terminal_read`**: Use to read output buffers or stdout streams from previously executed background terminal jobs or running services.
* **`ctx_terminal_write`**: Use to send input, keystrokes, or interactive responses to an active, running background process or terminal session.

## 4. Session & Context Management
* **`ctx_session_status`**: Use to inspect current context window usage, active tool state, and local SQLite storage stats.
* **`ctx_clear_session`**: Use when switching tasks, starting a completely unrelated debugging session, or when previous context history needs to be flushed from the local database.

## 5. Memory & Content Retrieval
* **`ctx_fetch_content`**: Use to retrieve specific sandboxed content blocks, error traces, or code snippets previously indexed into local SQLite storage via hash or ID.
* **`ctx_save_memory`**: Use to persist critical context insights, architectural decisions, or complex bug resolution notes across multi-turn agent conversations.