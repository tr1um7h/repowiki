# repowiki Architecture Design

**Version**: 0.4.0  
**Date**: 2026-09-08  
**Authors**: repowiki Project Team

---

## Table of Contents

- [Core Design Philosophy](#core-design-philosophy)
- [Overall Architecture](#overall-architecture)
- [Three-Phase Task Flow](#three-phase-task-flow)
- [Agent and Tool Collaboration](#agent-and-tool-collaboration)
- [File Selection Mechanism](#file-selection-mechanism)
- [Concurrency and Reliability Design](#concurrency-and-reliability-design)
- [End-to-End Complete Flow](#end-to-end-complete-flow)
- [Key Design Decisions](#key-design-decisions)

---

## Core Design Philosophy

### Deterministic Build System

**Core Concept**: repowiki itself does not contain LLM; it is a deterministic task orchestrator.

```
Traditional Approach:
  Cloud Service (Built-in LLM) → Read Code → Write Docs
  ↑ Integrated, inseparable, quality is a black box

repowiki Approach:
  Agent (Any AI/Human) → Read Code → Write Docs
       ↑                        ↓
  Deterministic Framework (repowiki) ← Validation Rules + Template Constraints
  ↑ Separated, composable, quality controlled
```

**Key Principles**:

1. **Agent handles intelligent work**: Read code, understand code, write documentation
2. **repowiki handles deterministic work**: Plan tasks, validate outputs, auto-repair, assemble metadata
3. **Zero LLM dependency**: repowiki doesn't depend on any specific large language model
4. **Agent replaceable**: Can use Claude Code, OpenCode, manual operation, or any other executor

### Why This Design?

| Approach | LLM Selection | Tool Capability | Feedback Loop | Concurrency | Quality Assurance |
|----------|---------------|-----------------|---------------|-------------|-------------------|
| Cloud Service | Fixed | Limited | None | None | Black box |
| repowiki | Flexible | Rich | Multi-round | Safe | Deterministic validation |

**Essence**:
- Agent = "Brain + Hands" (Intelligence + Execution)
- repowiki = "Ruler + Templates" (Framework + Constraints)

---

## Overall Architecture

### Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: Human (User)                                   │
│ - Request: "Generate repo wiki"                         │
│ - Monitor progress                                      │
│ - Final acceptance                                      │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 2: Agent (Executor)                               │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Prompt System                                  │     │
│  │ - System prompt                                │     │
│  │ - Task prompt (from repowiki task spec)       │     │
│  │ - Context management                           │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Tool System                                    │     │
│  │ - Read: Read code files                       │     │
│  │ - Write: Write documentation files            │     │
│  │ - Edit: Fix documentation                     │     │
│  │ - Bash: Call repowiki CLI                     │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Harness (Control Loop)                         │     │
│  │ - Observe: Observe environment                │     │
│  │ - Think: LLM reasoning and decision           │     │
│  │ - Act: Call tools                             │     │
│  │ - Feedback: Receive feedback                  │     │
│  │ - Loop: Iterate execution                     │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ LLM (Large Language Model)                     │     │
│  │ - Claude 3.5 Sonnet / GPT-4 / Others          │     │
│  │ - Text generation capability                  │     │
│  │ - Reasoning capability                        │     │
│  │ - Tool selection decision                     │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 3: repowiki CLI (Deterministic Tool)              │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Phase 1: Catalog                               │     │
│  │ - scanner: Scan repository files              │     │
│  │ - tasks: Generate catalog planning task       │     │
│  │ - Validate: JSON Schema validation            │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Phase 2: Page                                  │     │
│  │ - tasks: Generate page writing tasks          │     │
│  │ - validate: Validate outputs                  │     │
│  │ - Auto-fix: Anchors, line numbers, H1         │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Phase 3: Overview                              │     │
│  │ - tasks: Generate overview writing task       │     │
│  │ - metadata: Assemble metadata                 │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ State Management                               │     │
│  │ - state: Task state (index.json)              │     │
│  │ - claim: Atomic claim mechanism               │     │
│  │ - lock: Concurrency lock                      │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
│  ┌───────────────────────────────────────────────┐     │
│  │ Output Artifacts                               │     │
│  │ - content/: Wiki pages                        │     │
│  │ - meta/: Metadata                             │     │
│  │ - knowledge/: Knowledge cards                 │     │
│  │ - wiki.html: Single-file offline site         │     │
│  └───────────────────────────────────────────────┘     │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Key Design Principles

1. **Separation of Concerns**: Agent handles intelligence, repowiki handles determinism
2. **Tooling**: repowiki is a pure tool, not bound to any Agent
3. **Determinism**: Same input produces same output (validation, repair, assembly)
4. **Concurrency Safety**: Atomic claims + expiration recovery + heartbeat renewal
5. **Progressive**: Support partial completion, resume from checkpoint, incremental updates

---

## Three-Phase Task Flow

### Phase 1: Catalog (Directory Planning)

**Goal**: Plan the Wiki directory tree structure

**Input**:
- Repository file list (scanner scan)
- Repository information (REPO_NAME, KEY_FILES)
- File tree summary

**Process**:

```
┌─────────────────────────────────────────────────────┐
│ 1. Scanner scans repository                          │
│    - git ls-files or filesystem traversal           │
│    - Identify code files (by extension)             │
│    - Count LOC, identify key files                  │
│    - Generate file tree summary (max 20 per dir)    │
│    - Code file list (max 800, truncate if exceeded) │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 2. Tasks generate catalog task spec                  │
│    - Template: templates/en/catalog_task.md          │
│    - Inject: file list, repo info                   │
│    - Output: state/tasks/catalog.md                 │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 3. Agent executes catalog task                       │
│    - Read: state/tasks/catalog.md                    │
│    - LLM: Plan directory tree (JSON Schema)         │
│    - Write: state/catalog.json                       │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 4. repowiki validates catalog                        │
│    - JSON Schema validation                          │
│    - dependent_files path validation                │
│    - title/slug/id uniqueness validation            │
│    - Tree depth validation (≤ 4)                    │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 5. Tasks expand page tasks                           │
│    - Extract nodes from catalog.json                │
│    - Generate page task spec for each node          │
│    - Output: state/tasks/{id}.md                    │
└─────────────────────────────────────────────────────┘
```

**Agent Decisions**:

In the catalog task, Agent needs to:
1. **Understand repository structure**: Based on file list, file tree, key files
2. **Divide chapters**: By theme, not mechanically by directory
3. **Select files**: 3-12 most representative files per page
4. **Write page_brief**: Content points the page should cover

**Key Constraints**:

```json
{
  "repo_name": "<repo-name>",
  "chapters": [
    {
      "id": "c01",
      "title": "<chapter-title>",
      "slug": "<english-slug>",
      "summary": "<one-sentence-description>",
      "kind": "chapter",
      "dependent_files": ["<path>"],  // 3-12 files
      "page_brief": "<content-points>",
      "children": [...]
    }
  ]
}
```

### Phase 2: Page (Page Writing)

**Goal**: Write Wiki content page by page

**Input**:
- Task spec (state/tasks/{id}.md)
- Reference files (dependent_files)
- Page template, style guide

**Process**:

```
┌─────────────────────────────────────────────────────┐
│ 1. Agent claims task                                 │
│    - Bash: repowiki next . --claim --json            │
│    - Return: tasks[0].instructions                   │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 2. Agent understands task                            │
│    - Read: state/tasks/{id}.md                       │
│    - LLM: Understand what to write, constraints      │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 3. Agent reads code files                            │
│    - Read: dependent_files[0]                        │
│    - Read: dependent_files[1]                        │
│    - ...                                             │
│    - LLM: Understand each file's content            │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 4. Agent understands code structure                  │
│    - LLM: Analyze code structure                    │
│    - LLM: Extract key classes, functions, flows     │
│    - LLM: Generate document outline                 │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 5. Agent generates document                          │
│    - LLM: Based on code content + template + style  │
│    - Generate: markdown text                        │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 6. Agent writes file                                 │
│    - Write: .repowiki/en/content/{path}.md           │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 7. Agent validates                                   │
│    - Bash: repowiki check . --task {id}              │
│    - Return: ok: true/false + errors                 │
└─────────────────────────────────────────────────────┘
                         ↓
          ┌────────────────────────────────┐
          │ ok: true?                      │
          └────────────────────────────────┘
             │                    │
            Yes                   No
             │                    │
             ↓                    ↓
    ┌────────────────┐   ┌─────────────────────┐
    │ Task complete  │   │ 8. Agent fixes       │
    │                │   │ - Read: document     │
    │                │   │ - LLM: Based on      │
    │                │   │   errors             │
    │                │   │ - Write: fixed       │
    │                │   │   content            │
    │                │   │ - Re-check           │
    │                │   └─────────────────────┘
    └────────────────┘            ↓
                          (Loop until ok: true)
```

**Agent's Actual Work**:

A page task's actual execution includes:

```
Tool Calls: 10-15 times
  - Read: 4-7 times (read code files)
  - Write: 1-3 times (write document + fixes)
  - Bash: 2-3 times (check + retry)

LLM Reasoning: 10-15 times
  - Understand task spec: 1 time
  - Understand code files: 4-7 times
  - Generate document: 1 time
  - Fix (if needed): 2-3 times

Token Consumption: ~50k-100k tokens
  - 10-20x of a single LLM call
```

**Validation Content**:

repowiki's deterministic validation:

1. **H1 check**: Must be the task-specified title
2. **Anchor fix**: Automatically correct Chinese anchor format
3. **Line number fix**: Automatically correct out-of-range line numbers
4. **Path validation**: Verify file:// referenced files exist
5. **Placeholder check**: Scan for unreplaced {{...}}
6. **Template compliance**: Check if required sections exist

**Auto-Fix**:

Deterministic defects are automatically fixed; Agent only needs to fix semantic issues:

```python
# validate.py
def auto_fix(content, task_spec):
    # 1. H1 fix
    content = fix_h1(content, task_spec["title"])
    
    # 2. Anchor fix
    content = fix_anchors(content)
    
    # 3. Line number fix
    content = fix_line_ranges(content, actual_file_lines)
    
    return content
```

### Phase 3: Overview

**Goal**: Write repository Wiki overview

**Input**:
- Directory tree (catalog_tree_text)
- Completed pages

**Process**:

```
┌─────────────────────────────────────────────────────┐
│ 1. finalize creates overview task                    │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 2. Agent writes overview                             │
│    - Read: Some representative pages                │
│    - LLM: Write repo positioning, chapter nav,      │
│           usage guide                                │
│    - Write: .repowiki/en/meta/wiki-overview.md      │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 3. repowiki assembles metadata.json                  │
│    - Collect all pages                              │
│    - Collect overview                               │
│    - Collect repo info                              │
│    - Generate: .repowiki/en/meta/repowiki-metadata  │
│                .json                                │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ 4. Clean up task state                               │
│    - Delete: state/claims/                          │
│    - Delete: state/tasks/                           │
│    - Keep: state/catalog.json, state/index.json     │
└─────────────────────────────────────────────────────┘
```

---

## Agent and Tool Collaboration

### Complete Agent Capability Model

**Agent = LLM + Tools + Prompt + Harness**

```
┌──────────────────────────────────────────────────────┐
│ Agent Capability Model                                │
├──────────────────────────────────────────────────────┤
│                                                      │
│ 1. Prompt System                                     │
│    - System prompt: Role, capabilities, constraints  │
│    - Task prompt: From repowiki task spec            │
│    - Context: Conversation history, file content     │
│                                                      │
│ 2. Tool System                                       │
│    - Read: Read code files, task specs               │
│    - Write: Write documentation files                │
│    - Edit: Fix documentation                         │
│    - Bash: Call repowiki CLI                         │
│    - WebFetch: Get external info (optional)          │
│                                                      │
│ 3. Harness (Control Loop)                            │
│    - Observe: Observe environment, read info         │
│    - Think: LLM reasoning, decision                  │
│    - Act: Select and call tools                      │
│    - Feedback: Receive tool return results           │
│    - Loop: Decide next step based on feedback        │
│                                                      │
│ 4. LLM (Large Language Model)                        │
│    - Text generation: Understand, generate docs      │
│    - Reasoning: Analyze code structure, plan content │
│    - Tool selection: Decide which tool to call       │
│                                                      │
└──────────────────────────────────────────────────────┘
```

### Actual Collaboration Flow (Single Task)

```
User: "Generate repo wiki"
  ↓
Agent starts
  ↓
┌──────────────────────────────────────────────────────┐
│ Loop 1: Planning                                      │
├──────────────────────────────────────────────────────┤
│ Observe: Repository file list                        │
│ Think: Need to plan directory first                  │
│ Act: Bash("repowiki plan .")                         │
│ Feedback: catalog task created                       │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Loop 2: Execute catalog                               │
├──────────────────────────────────────────────────────┤
│ Act: Bash("repowiki next . --claim --json")          │
│ Feedback: Returns catalog task spec                   │
│ Act: Read("state/tasks/catalog.md")                  │
│ Think: Understand to plan directory tree             │
│ Act: Read(multiple key files)                        │
│ Think: Analyze repo structure, divide chapters       │
│ Act: Write("state/catalog.json", plan result)        │
│ Act: Bash("repowiki check . --task catalog")         │
│ Feedback: ok: true                                   │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Loop 3-50: Execute page tasks (loop)                 │
├──────────────────────────────────────────────────────┤
│ Act: Bash("repowiki next . --claim --json")          │
│ Feedback: Returns page task                          │
│ Act: Read("state/tasks/{id}.md")                     │
│ Act: Read(4-7 code files)                            │
│ Think: Understand code structure, generate doc       │
│ Act: Write("document content")                       │
│ Act: Bash("repowiki check . --task {id}")            │
│ Feedback: ok: true/false                             │
│ (If failed, Loop to fix)                             │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Loop: Execute overview task                           │
├──────────────────────────────────────────────────────┤
│ ...similar process...                                │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Loop: Finalize                                        │
├──────────────────────────────────────────────────────┤
│ Act: Bash("repowiki finalize .")                     │
│ Feedback: metadata.json generated                    │
└──────────────────────────────────────────────────────┘
  ↓
Complete
```

---

## File Selection Mechanism

### Core Question

A directory has 35 files, select all or a subset?

**Answer: Select a subset (3-12 files)**

### Selection Process

```
┌─────────────────────────────────────────────────────┐
│ Phase 1: Catalog                                    │
├─────────────────────────────────────────────────────┤
│ repowiki provides:                                  │
│   - File list (max 800 code files)                  │
│   - File tree summary (max 20 files per directory)  │
│   - LOC, language, key file markers                 │
│                                                     │
│ Agent decides:                                      │
│   - Understand repository structure                 │
│   - Divide chapters                                 │
│   - Select 3-12 most representative files per page  │
│   - Based on: filename, LOC, importance, page_brief │
│                                                     │
│ Output:                                             │
│   - catalog.json                                    │
│   - Each node's dependent_files: 3-12 files         │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ Phase 2: Page                                       │
├─────────────────────────────────────────────────────┤
│ repowiki provides:                                  │
│   - Task spec (state/tasks/{id}.md)                 │
│   - hint_files = dependent_files from catalog       │
│                                                     │
│ Agent executes:                                     │
│   - Read: All hint_files (4-7 files)                │
│   - LLM: Understand code content                    │
│   - LLM: Generate document                          │
│   - Write: Document file                            │
│                                                     │
│ Final references:                                   │
│   - <cite> block lists actually referenced files:   │
│     3-15 files                                      │
│   - Selected from hint_files                        │
└─────────────────────────────────────────────────────┘
```

### Selection Criteria

How does Agent select 3-12 files from 35 files in catalog phase?

```
Priority criteria:
1. Filename meaning
   - main.py, core.py, __init__.py > utils.py, helpers.py
   - models.py, views.py, controllers.py > tests_*.py

2. LOC (lines of code)
   - 300 lines > 50 lines
   - Larger files are usually more core

3. Directory location
   - Root directory, core directories > edge directories
   - src/core/ > src/utils/

4. File type
   - Core logic > utility functions > tests
   - models.py > utils.py > test_models.py

5. page_brief
   - Select files based on content points the page should cover
   - "User authentication flow" → login.py, register.py, auth_middleware.py
```

### Real Example

```
Directory: src/auth/ (35 files)

File list:
  src/auth/login.py (250 lines)
  src/auth/logout.py (80 lines)
  src/auth/register.py (300 lines)
  src/auth/middleware.py (150 lines)
  src/auth/utils.py (100 lines)
  src/auth/validators.py (120 lines)
  src/auth/models.py (180 lines)
  src/auth/api.py (200 lines)
  src/auth/config.py (90 lines)
  ... (26 more files)

Agent selection (Authentication System page):
  {
    "id": "auth",
    "title": "Authentication System",
    "dependent_files": [
      "src/auth/login.py",        ✓ Login core
      "src/auth/register.py",     ✓ Registration core
      "src/auth/middleware.py",   ✓ Auth middleware
      "src/auth/models.py",       ✓ Data models
      "src/auth/config.py",       ✓ Configuration
      "src/auth/api.py",          ✓ API endpoints
      "src/auth/validators.py"    ✓ Validation logic
    ],
    "page_brief": "Login flow, registration flow, auth middleware, user models, config"
  }

Result: 7 files (not all 35)
```

### Why Not Select All?

```
Problem 1: Context limit
  - 35 files × avg 150 lines = 5250 lines of code
  - Token consumption: ~50k tokens
  - May not fit in LLM context

Problem 2: Noise interference
  - Utility functions, test files are not core
  - Redundant information lowers document quality

Problem 3: Cost control
  - Selecting representative files reduces token consumption
  - 10-20x token difference

Solution:
  - Agent selects representative files in catalog phase
  - Quality over quantity
```

---

## Concurrency and Reliability Design

### Concurrency Safety Mechanism

```
┌─────────────────────────────────────────────────────┐
│ Atomic Claim                                         │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Agent A: mkdir claims/auth.AgentA                  │
│           ✓ Success, obtained claim                 │
│                                                     │
│  Agent B: mkdir claims/auth.AgentB                  │
│           ✗ Failed, ConflictError                   │
│                                                     │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ Heartbeat Renewal                                    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Agent A: Claims task auth                          │
│           ↓                                         │
│  Every 3 minutes: touch --task auth --worker AgentA │
│           ↓ Updates mtime                           │
│  Prevents expiration recovery                       │
│                                                     │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ Expiration Recovery                                  │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Claim directory mtime exceeds 15 minutes           │
│           ↓                                         │
│  next --claim auto-detects                          │
│           ↓                                         │
│  Rename: claims/auth.AgentA → claims/stale-AgentA   │
│           ↓                                         │
│  Task re-queued: status: pending                    │
│           ↓                                         │
│  Other agents can claim                             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## End-to-End Complete Flow

### Scenario: Generate Wiki for a Repository

A detailed walkthrough from user request to final output is documented in the Chinese version [ARCHITECTURE.md](../zh/ARCHITECTURE.md). The English version follows the same process with locale differences.

**Key Statistics** (typical repo with 50 page tasks):

```
Total Tool Calls:
  - Read: ~250-350 times
  - Write: ~50-150 times
  - Bash: ~200-300 times
  - Total: ~500-800 tool calls

Total LLM Reasoning:
  - ~500-800 LLM calls

Token Consumption:
  - ~2.5M-5M tokens

Time Consumption:
  - Serial: ~2-4 hours
  - Concurrent (4 agents): ~30-60 minutes

Cost Estimate (Claude 3.5 Sonnet):
  - Serial: ~$10-20
  - Concurrent: ~$10-20 (same tokens, faster)
```

---

## Key Design Decisions

### 1. Why Not Built-in LLM?

```
Advantages:
  ✓ Agent replaceable (Claude Code, OpenCode, manual)
  ✓ LLM selectable (Claude, GPT-4, local models)
  ✓ Rich tools (Read, Write, Bash, WebFetch)
  ✓ Multi-round feedback (check → fix → check)
  ✓ Concurrency safe (multi-agent collaboration)

Disadvantages:
  ✗ Need to install Agent
  ✗ Need to understand Agent workflow

Conclusion:
  Separation of concerns, deterministic framework + intelligent
  execution = best quality
```

### 2. Why Select 3-12 Files?

```
Factor 1: Context limit
  - Too many files exceed LLM context
  - Code files average 150 lines
  - 12 files × 150 lines = 1800 lines ≈ 18k tokens
  - Within context window

Factor 2: Quality control
  - Quality over quantity
  - Representative files more focused on core
  - Avoid noise lowering document quality

Factor 3: Cost control
  - 10-20x token difference
  - 12 files vs 35 files

Conclusion:
  3-12 files represents balance of quality, cost, context
```

### 3. Why Need Deterministic Validation?

```
LLM non-determinism:
  - Same input, different outputs
  - H1 may be wrong
  - Line numbers may exceed range
  - Anchor format may be incorrect

repowiki determinism:
  - H1 must be task-specified title
  - Line numbers auto-corrected
  - Anchors auto-fixed
  - Paths must exist

Conclusion:
  Deterministic validation + auto-repair = quality assurance
```

### 4. Why Need Concurrency Safety?

```
Large repository scenario:
  - 50 page tasks
  - Serial: 2-4 hours
  - Concurrent (4 agents): 30-60 minutes

Concurrency risks:
  - Multiple agents claim same task
  - Data races
  - Deadlocks

Solution:
  - Atomic claims (mkdir)
  - Heartbeat renewal (touch)
  - Expiration recovery (stale)
  - File locks (.index.lock)

Conclusion:
  Atomic operations + heartbeat mechanism = concurrency safety
```

---

## Summary

### Core Philosophy

```
Agent = LLM + Tools + Prompt + Harness
repowiki = Deterministic framework + Quality assurance

Agent handles intelligence: Read code, understand, write docs
repowiki handles determinism: Plan, validate, repair, assemble

Separation = Replaceable + High quality + Concurrency safe
```

### Architecture Advantages

1. **Zero LLM dependency**: repowiki doesn't bind to any large language model
2. **Agent replaceable**: Claude Code, OpenCode, manual all work
3. **Deterministic quality**: Template enforcement + programmatic validation
4. **Concurrency safe**: Atomic claims + heartbeat mechanism
5. **Progressive experience**: Partial completion viewable

### Design Philosophy

> **Agent handles intelligence, repowiki handles reliability**

- LLM is "neuron"
- Agent is "brain + body"
- repowiki is "ruler and templates"

---

**Document Version**: 0.4.0  
**Last Updated**: 2026-09-08  
**Maintainers**: repowiki Project Team