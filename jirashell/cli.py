#!/usr/bin/env python3
"""
Jira interactive CLI.

Usage:
    python3 jira_cli.py

Commands (context-sensitive):
    search <keyword> [last <N> days|weeks|months]
    list [last <N> days|weeks|months]
    select <number>
    tickets
    view <number>
    next / prev
    back
    refresh
    help
    exit
"""
import argparse
import configparser
import os
import termios
import tty
import re
import sys
import readline
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jirashell.client import JiraClient

CONFIG_PATH = os.path.expanduser("~/.jira")

# ---------------------------------------------------------------------------
# Config (~/.jira)
# ---------------------------------------------------------------------------

def load_config():
    """Read credentials from ~/.jira. Returns (domain, email, api_key) or None."""
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_PATH):
        return None
    section = "default"
    if not cfg.has_section(section):
        return None
    try:
        return (
            cfg.get(section, "domain"),
            cfg.get(section, "email"),
            cfg.get(section, "api_key"),
        )
    except configparser.NoOptionError:
        return None


def save_config(domain, email, api_key):
    cfg = configparser.ConfigParser()
    cfg["default"] = {"domain": domain, "email": email, "api_key": api_key}
    with open(CONFIG_PATH, "w") as f:
        cfg.write(f)
    os.chmod(CONFIG_PATH, 0o600)


def run_configure(existing=None):
    """
    Interactive configuration wizard (à la s3cmd --configure).
    existing: (domain, email, api_key) tuple to use as defaults, or None.
    Returns (domain, email, api_key) after saving.
    """
    ex = existing or (None, None, None)

    print("\nJira CLI — Configuration Wizard")
    print("═" * 44)
    print(f"Credentials will be saved to {CONFIG_PATH} (mode 0600).\n")
    print("  Your Jira subdomain: e.g. 'acme' for acme.atlassian.net")
    print("  API token:  create one at id.atlassian.com → Security → API tokens\n")

    def _prompt(label, default=None, secret=False, visible_tail=2):
        if secret:
            hint = f" [{'*' * 6}{default[-visible_tail:]}]" if default else ""
        else:
            hint = f" [{default}]" if default else ""
        prompt_str = f"  {label}{hint}: "

        if not secret:
            raw = input(prompt_str).strip()
            return raw if raw else default

        # Starred input: echo * for each char, last `visible_tail` shown as-is.
        import shutil
        term_cols = shutil.get_terminal_size().columns
        sys.stdout.write(prompt_str)
        sys.stdout.flush()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        chars = []
        # Tracks total columns currently on screen (prompt + masked value).
        # Used to calculate how many wrapped lines to move up before redrawing.
        cur_len = [len(prompt_str)]

        def _render():
            n = len(chars)
            masked = "".join(chars) if n <= visible_tail \
                else "*" * (n - visible_tail) + "".join(chars[-visible_tail:])
            lines_up = max(0, (cur_len[0] - 1) // term_cols)
            up = f"\033[{lines_up}A" if lines_up else ""
            sys.stdout.write(f"{up}\r{prompt_str}{masked}\033[J")
            sys.stdout.flush()
            cur_len[0] = len(prompt_str) + len(masked)

        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    break
                elif ch in ("\x7f", "\x08"):  # backspace / delete
                    if chars:
                        chars.pop()
                        _render()
                elif ch == "\x03":  # Ctrl-C
                    raise KeyboardInterrupt
                elif ch == "\x04":  # Ctrl-D / EOF
                    break
                elif ch >= " ":  # printable
                    chars.append(ch)
                    _render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        sys.stdout.write("\n")
        sys.stdout.flush()
        raw = "".join(chars)
        return raw if raw else default

    domain  = _prompt("Jira subdomain", default=ex[0])
    email   = _prompt("Atlassian email", default=ex[1])
    api_key = _prompt("API token", default=ex[2], secret=True)

    if not all([domain, email, api_key]):
        print("\n  All fields are required. Configuration not saved.")
        sys.exit(1)

    print("\n  Testing connection...", end=" ", flush=True)
    try:
        JiraClient(domain, email, api_key)._get("myself")
        print("OK")
    except Exception as e:
        print(f"FAILED\n  {e}")
        ans = input("  Save configuration anyway? [y/N]: ").strip().lower()
        if ans != "y":
            print("  Configuration not saved.")
            sys.exit(1)

    save_config(domain, email, api_key)
    print(f"\n  Saved to {CONFIG_PATH} (permissions: 0600).")
    return domain, email, api_key


COMMANDS = [
    "search", "list", "mine", "select", "tickets", "view",
    "next", "prev", "back", "home", "refresh",
    "create", "transition", "comment",
    "boards", "board",
    "help", "exit", "quit",
]

def _completer(text, state):
    options = [c for c in COMMANDS if c.startswith(text)]
    return options[state] if state < len(options) else None

readline.parse_and_bind("tab: complete")
readline.set_completer(_completer)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIME_RE = re.compile(
    r"(?:last\s+)?(\d+)\s+(days?|weeks?|months?)",
    re.IGNORECASE,
)

PAGE_SIZE = 15


def parse_time_args(tokens: list[str]) -> tuple[dict, list[str]]:
    """
    Extract a time expression from tokens.
    Returns (time_kwargs, remaining_tokens).
    Example: ["payments", "last", "3", "months"] -> ({"months": 3}, ["payments"])
    """
    joined = " ".join(tokens)
    m = TIME_RE.search(joined)
    if not m:
        return {}, tokens
    n = int(m.group(1))
    unit = m.group(2).lower().rstrip("s")
    key = {"day": "days", "week": "weeks", "month": "months"}[unit]
    remainder = (joined[: m.start()] + joined[m.end() :]).split()
    return {key: n}, remainder


def fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def fmt_user(user_field) -> str:
    if not user_field:
        return "Unassigned"
    return user_field.get("displayName", "?")


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Color
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    "done": "92", "closed": "92", "resolved": "92",
    "complete": "92", "completed": "92",
    "in progress": "36", "in review": "36", "in development": "36",
    "in qa": "36", "under review": "36",
    "blocked": "91", "impediment": "91",
    "to do": "2", "open": "2", "backlog": "2", "new": "2", "reopened": "2",
}

_PRIORITY_COLORS = {
    "highest": "91", "critical": "91",
    "high": "33",
    "low": "2", "lowest": "2",
}


def _ansi(text, code):
    return f"\033[{code}m{text}\033[0m"


def color_status(name: str, width: int = 0) -> str:
    """Return name padded to width, wrapped in ANSI color if recognised."""
    padded = f"{name:<{width}}" if width else name
    code = _STATUS_COLORS.get(name.strip().lower())
    return _ansi(padded, code) if code else padded


def color_priority(name: str, width: int = 0) -> str:
    padded = f"{name:<{width}}" if width else name
    code = _PRIORITY_COLORS.get(name.strip().lower())
    return _ansi(padded, code) if code else padded


# ---------------------------------------------------------------------------
# View dataclass
# ---------------------------------------------------------------------------

@dataclass
class View:
    state: str                      # "home" | "epics" | "tickets" | "ticket"
    data: Any = None                # fetched payload for this view
    page: int = 0
    context: dict = field(default_factory=dict)  # keyword, epic_key, ticket_key, time_kwargs
    cache_key: str = ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class JiraCLI:
    def __init__(self, domain, email, api_key):
        self.client = JiraClient(domain, email, api_key)
        self.nav_stack: list[View] = [View(state="home")]

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    def run(self):
        print("Jira CLI  —  type 'help' for available commands, 'exit' to quit.\n")
        self._display_current()
        while True:
            try:
                raw = input(self._prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break
            if not raw:
                continue
            self._dispatch(raw)

    def _prompt(self) -> str:
        parts = []
        for v in self.nav_stack:
            if v.state == "home":
                parts.append("home")
            elif v.state == "epics":
                kw = v.context.get("keyword") or "*"
                parts.append(f"epics:{kw}")
            elif v.state == "tickets":
                parts.append(v.context.get("epic_key", "?"))
            elif v.state == "mine":
                parts.append("mine")
            elif v.state == "ticket":
                parts.append(v.context.get("ticket_key", "?"))
            elif v.state == "boards":
                parts.append("boards")
            elif v.state == "kanban":
                parts.append(f"board:{v.context.get('board_name', '?')}")
        return " > ".join(parts) + " $ "

    def _dispatch(self, raw: str):
        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        # Allow bare numbers as shortcuts
        if parts[0].isdigit():
            state = self.nav_stack[-1].state
            if state == "epics":
                self.cmd_select(parts)
            elif state in ("tickets", "mine", "kanban"):
                self.cmd_view(parts)
            elif state == "boards":
                self.cmd_board(parts)
            else:
                print("Type a command. Use 'help' for options.")
            return

        handlers = {
            "search": self.cmd_search,
            "list": self.cmd_list,
            "mine": self.cmd_mine,
            "select": self.cmd_select,
            "tickets": lambda _: self.cmd_tickets(),
            "view": self.cmd_view,
            "next": lambda _: self.cmd_next(),
            "prev": lambda _: self.cmd_prev(),
            "back": lambda _: self.cmd_back(),
            "home": lambda _: self.cmd_home(),
            "create": self.cmd_create,
            "transition": lambda _: self.cmd_transition(),
            "comment": lambda _: self.cmd_comment(),
            "boards": self.cmd_boards,
            "board": self.cmd_board,
            "refresh": lambda _: self.cmd_refresh(),
            "help": lambda _: self.cmd_help(),
            "exit": lambda _: (print("Goodbye."), sys.exit(0)),
            "quit": lambda _: (print("Goodbye."), sys.exit(0)),
        }
        handler = handlers.get(cmd)
        if handler:
            handler(args)
        else:
            print(f"Unknown command '{cmd}'. Type 'help' for options.")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def cmd_search(self, args: list[str]):
        if not args:
            print("Usage: search <keyword> [last <N> days|weeks|months]")
            return
        time_kwargs, keyword_tokens = parse_time_args(args)
        keyword = " ".join(keyword_tokens).strip() or None

        label = f"'{keyword}'" if keyword else "all projects"
        print(f"Searching epics for {label}...")
        try:
            epics = self.client.get_epics(keyword=keyword, **time_kwargs)
        except Exception as e:
            print(f"Error: {e}")
            return

        view = View(
            state="epics",
            data=epics,
            context={"keyword": keyword, "time_kwargs": time_kwargs},
            cache_key=f"epics|{keyword}|{time_kwargs}",
        )
        self.nav_stack.append(view)
        self._display_current()

    def cmd_list(self, args: list[str]):
        time_kwargs, _ = parse_time_args(args)
        label = ""
        if "days" in time_kwargs:
            label = f" from the last {time_kwargs['days']} days"
        elif "weeks" in time_kwargs:
            label = f" from the last {time_kwargs['weeks']} weeks"
        elif "months" in time_kwargs:
            label = f" from the last {time_kwargs['months']} months"

        print(f"Fetching epics{label}...")
        try:
            epics = self.client.get_epics(**time_kwargs)
        except Exception as e:
            print(f"Error: {e}")
            return

        view = View(
            state="epics",
            data=epics,
            context={"keyword": None, "time_kwargs": time_kwargs},
            cache_key=f"epics|None|{time_kwargs}",
        )
        self.nav_stack.append(view)
        self._display_current()

    def cmd_mine(self, args: list[str]):
        time_kwargs, _ = parse_time_args(args)
        print("Fetching tickets assigned to you...")
        try:
            tickets = self.client.get_my_issues(**time_kwargs)
        except Exception as e:
            print(f"Error: {e}")
            return
        view = View(
            state="mine",
            data=tickets,
            context={"epic_key": "", "epic_summary": "My Tickets", "time_kwargs": time_kwargs},
            cache_key=f"mine|{time_kwargs}",
        )
        self.nav_stack.append(view)
        self._display_current()

    def cmd_transition(self):
        view = self.nav_stack[-1]
        if view.state != "ticket":
            print("Navigate into a ticket first.")
            return
        key = view.context["ticket_key"]
        print(f"Fetching available transitions for {key}...")
        try:
            transitions = self.client.get_transitions(key)
        except Exception as e:
            print(f"Error: {e}")
            return
        if not transitions:
            print("No transitions available.")
            return

        print(f"\n  ── Transitions for {key} " + "─" * 36)
        for i, t in enumerate(transitions, 1):
            print(f"  {i}. {t['name']}")
        print()

        raw = self._prompt_input("Select transition number")
        try:
            idx = int(raw) - 1
            if not (0 <= idx < len(transitions)):
                raise ValueError()
        except (TypeError, ValueError):
            print("Invalid selection.")
            return

        chosen = transitions[idx]
        print(f"  Transitioning to '{chosen['name']}'...")
        try:
            self.client.do_transition(key, chosen["id"])
        except Exception as e:
            print(f"  Error: {e}")
            return

        print(f"  Done. Refreshing ticket...")
        self.client.invalidate(view.cache_key)
        try:
            view.data = self.client.get_ticket(key)
        except Exception as e:
            print(f"  Error refreshing: {e}")
            return
        self._display_current()

    def cmd_comment(self):
        view = self.nav_stack[-1]
        if view.state != "ticket":
            print("Navigate into a ticket first.")
            return
        key = view.context["ticket_key"]
        print(f"\n  ── Add Comment to {key} " + "─" * 38)
        text = self._prompt_multiline("Comment")
        if not text:
            print("  Empty comment, nothing posted.")
            return
        print("  Posting comment...")
        try:
            self.client.add_comment(key, text)
        except Exception as e:
            print(f"  Error: {e}")
            return
        print("  Comment posted. Refreshing ticket...")
        self.client.invalidate(view.cache_key)
        try:
            view.data = self.client.get_ticket(key)
        except Exception as e:
            print(f"  Error refreshing: {e}")
            return
        self._display_current()

    def cmd_select(self, args: list[str]):
        view = self.nav_stack[-1]
        if view.state != "epics" or not view.data:
            print("No epic list loaded. Use 'search' or 'list' first.")
            return
        try:
            idx = int(args[0]) - 1
            if not (0 <= idx < len(view.data)):
                raise ValueError()
        except (IndexError, ValueError):
            print(f"Enter a number between 1 and {len(view.data)}.")
            return

        epic = view.data[idx]
        key = epic["key"]
        summary = epic["fields"].get("summary", "")
        print(f"\nLoading tickets for [{key}] {summary} ...")
        try:
            tickets = self.client.get_tickets(key)
        except Exception as e:
            print(f"Error: {e}")
            return

        self.nav_stack.append(View(
            state="tickets",
            data=tickets,
            context={"epic_key": key, "epic_summary": summary},
            cache_key=f"tickets|{key}",
        ))
        self._display_current()

    def cmd_tickets(self):
        view = self.nav_stack[-1]
        if view.state != "tickets":
            print("Select an epic first with 'select <number>'.")
            return
        self._display_current()

    def cmd_view(self, args: list[str]):
        view = self.nav_stack[-1]
        if view.state not in ("tickets", "mine", "kanban") or not view.data:
            print("No ticket list loaded. Select an epic or board first.")
            return

        if view.state == "kanban":
            flat = view.context.get("flat_issues", [])
            try:
                idx = int(args[0]) - 1
                if not (0 <= idx < len(flat)):
                    raise ValueError()
            except (IndexError, ValueError):
                print(f"Enter a number between 1 and {len(flat)}.")
                return
            ticket = flat[idx]
            key = ticket["key"]
            print(f"Loading {key}...")
            try:
                details = self.client.get_ticket(key)
            except Exception as e:
                print(f"Error: {e}")
                return
            self.nav_stack.append(View(
                state="ticket",
                data=details,
                context={"ticket_key": key, "ticket_idx": idx},
                cache_key=f"ticket|{key}",
            ))
            self._display_current()
            return

        try:
            idx = int(args[0]) - 1
            if not (0 <= idx < len(view.data)):
                raise ValueError()
        except (IndexError, ValueError):
            print(f"Enter a number between 1 and {len(view.data)}.")
            return

        ticket = view.data[idx]
        key = ticket["key"]
        print(f"Loading {key}...")
        try:
            details = self.client.get_ticket(key)
        except Exception as e:
            print(f"Error: {e}")
            return

        self.nav_stack.append(View(
            state="ticket",
            data=details,
            context={"ticket_key": key, "ticket_idx": idx},
            cache_key=f"ticket|{key}",
        ))
        self._display_current()

    def cmd_boards(self, args: list[str]):
        project = args[0].upper() if args else None
        label = f" for project {project}" if project else ""
        print(f"Fetching kanban boards{label}...")
        try:
            boards = self.client.get_boards(project_key=project)
        except Exception as e:
            print(f"Error: {e}")
            return
        view = View(
            state="boards",
            data=boards,
            context={"project": project},
            cache_key=f"boards|{project}",
        )
        self.nav_stack.append(view)
        self._display_current()

    def cmd_board(self, args: list[str]):
        view = self.nav_stack[-1]
        if view.state != "boards" or not view.data:
            print("No board list loaded. Use 'boards' first.")
            return
        try:
            idx = int(args[0]) - 1
            if not (0 <= idx < len(view.data)):
                raise ValueError()
        except (IndexError, ValueError):
            print(f"Enter a number between 1 and {len(view.data)}.")
            return

        board = view.data[idx]
        board_id = board["id"]
        board_name = board["name"]
        print(f"Loading board [{board_name}]...")
        try:
            config = self.client.get_board_config(board_id)
            issues = self.client.get_board_issues(board_id)
        except Exception as e:
            print(f"Error: {e}")
            return

        columns, flat = self._build_kanban_columns(config, issues)
        self.nav_stack.append(View(
            state="kanban",
            data=columns,
            context={"board_id": board_id, "board_name": board_name, "flat_issues": flat},
            cache_key=f"board_issues|{board_id}",
        ))
        self._display_current()

    def _build_kanban_columns(self, config, issues):
        columns_cfg = config.get("columnConfig", {}).get("columns", [])
        status_to_col = {}
        for col in columns_cfg:
            for s in col.get("statuses", []):
                status_to_col[s["id"]] = col["name"]

        col_issues: dict[str, list] = {col["name"]: [] for col in columns_cfg}
        uncategorized = []
        for issue in issues:
            status_id = issue["fields"].get("status", {}).get("id")
            col_name = status_to_col.get(status_id)
            if col_name and col_name in col_issues:
                col_issues[col_name].append(issue)
            else:
                uncategorized.append(issue)

        columns = [{"name": col["name"], "issues": col_issues[col["name"]]} for col in columns_cfg]
        if uncategorized:
            columns.append({"name": "Other", "issues": uncategorized})

        flat = [issue for col in columns for issue in col["issues"]]
        return columns, flat

    def cmd_next(self):
        view = self.nav_stack[-1]
        if view.state in ("epics", "tickets", "mine") and view.data:
            max_page = max(0, (len(view.data) - 1) // PAGE_SIZE)
            if view.page < max_page:
                view.page += 1
                self._display_current()
            else:
                print("Already on the last page. Use 'select' or 'view' to drill in.")
        elif view.state == "ticket":
            # Navigate to the next ticket in the parent list
            parent = self._parent_view("tickets") or self._parent_view("mine")
            if parent:
                idx = view.context.get("ticket_idx", 0)
                if idx + 1 < len(parent.data):
                    self.nav_stack.pop()
                    self.nav_stack[-1] = parent
                    self.cmd_view([str(idx + 2)])  # 1-based
                else:
                    print("Already at the last ticket.")
        else:
            print("Nothing to navigate.")

    def cmd_prev(self):
        view = self.nav_stack[-1]
        if view.state in ("epics", "tickets", "mine") and view.data:
            if view.page > 0:
                view.page -= 1
                self._display_current()
            else:
                print("Already on the first page.")
        elif view.state == "ticket":
            parent = self._parent_view("tickets") or self._parent_view("mine")
            if parent:
                idx = view.context.get("ticket_idx", 0)
                if idx > 0:
                    self.nav_stack.pop()
                    self.nav_stack[-1] = parent
                    self.cmd_view([str(idx)])  # 1-based: idx is already prev
                else:
                    print("Already at the first ticket.")
        else:
            print("Nothing to navigate.")

    def cmd_back(self):
        if len(self.nav_stack) <= 1:
            print("Already at the top level.")
            return
        self.nav_stack.pop()
        print()
        self._display_current()

    def cmd_home(self):
        if len(self.nav_stack) <= 1:
            print("Already at home.")
            return
        self.nav_stack = self.nav_stack[:1]
        print()
        self._display_current()

    def cmd_create(self, args: list[str]):
        if not args:
            print("Usage: create epic | create ticket")
            return
        sub = args[0].lower()
        if sub == "epic":
            self._create_epic()
        elif sub in ("ticket", "issue"):
            self._create_ticket()
        else:
            print("Usage: create epic | create ticket")

    def _infer_project(self) -> str:
        """Infer project key from current nav context."""
        for v in reversed(self.nav_stack):
            if v.state == "tickets":
                epic_key = v.context.get("epic_key", "")
                return epic_key.split("-")[0] if "-" in epic_key else ""
            if v.state == "epics" and v.data:
                return v.data[0].get("fields", {}).get("project", {}).get("key", "")
            if v.state == "epics" and v.context.get("keyword"):
                return v.context["keyword"].upper()
        return ""

    def _prompt_input(self, label, default=None, required=False):
        """Prompt for a single line of input with an optional default."""
        hint = f" [{default}]" if default else ""
        value = input(f"  {label}{hint}: ").strip()
        if not value and default:
            return default
        if not value and required:
            print(f"  {label} is required.")
            return None
        return value or None

    def _prompt_multiline(self, label):
        """Prompt for multi-line input. Empty line finishes."""
        print(f"  {label} (press Enter on an empty line to finish):")
        lines = []
        while True:
            line = input("  > ")
            if not line:
                break
            lines.append(line)
        return "\n\n".join(lines) or None

    def _create_epic(self):
        default_project = self._infer_project()
        print("\n  ── Create Epic " + "─" * 44)

        project = self._prompt_input("Project key", default=default_project, required=True)
        if not project:
            return
        summary = self._prompt_input("Summary", required=True)
        if not summary:
            return
        description = self._prompt_multiline("Description (optional)")

        print(f"\n  Creating epic in {project.upper()}...")
        try:
            result = self.client.create_issue(
                project_key=project.upper(),
                summary=summary,
                issue_type="Epic",
                description=description,
            )
        except Exception as e:
            print(f"  Error: {e}")
            return

        key = result.get("key", "?")
        print(f"  Created: [{key}] {summary}")
        print(f"  Type 'search {project.lower()}' to see it, or 'refresh' to reload the current list.\n")
        self.client.invalidate()

    def _create_ticket(self):
        # Find epic context anywhere in the stack
        epic_view = next((v for v in reversed(self.nav_stack) if v.state == "tickets"), None)
        if not epic_view:
            print("  Navigate into an epic first (select an epic, then run 'create ticket').")
            return

        epic_key = epic_view.context.get("epic_key", "")
        project_key = epic_key.split("-")[0] if "-" in epic_key else self._infer_project()

        print(f"\n  ── Create Ticket in [{epic_key}] " + "─" * 28)

        summary = self._prompt_input("Summary", required=True)
        if not summary:
            return

        raw_type = self._prompt_input("Type", default="Story")
        issue_type = raw_type.strip().capitalize() if raw_type else "Story"
        if issue_type.lower() not in ("story", "task", "bug", "subtask"):
            print(f"  Unknown type '{issue_type}', defaulting to Story.")
            issue_type = "Story"

        description = self._prompt_multiline("Description (optional)")

        print(f"\n  Creating {issue_type} under {epic_key}...")
        try:
            result = self.client.create_issue(
                project_key=project_key,
                summary=summary,
                issue_type=issue_type,
                description=description,
                epic_key=epic_key,
            )
        except Exception as e:
            print(f"  Error: {e}")
            return

        key = result.get("key", "?")
        print(f"  Created: [{key}] {summary}")
        print(f"  Type 'refresh' to reload the ticket list.\n")
        self.client.invalidate(f"tickets|{epic_key}")

    def cmd_refresh(self):
        view = self.nav_stack[-1]
        self.client.invalidate(view.cache_key)
        if view.state == "boards":
            try:
                view.data = self.client.get_boards(project_key=view.context.get("project"))
            except Exception as e:
                print(f"Error: {e}")
                return
            view.page = 0
            print("Refreshed.\n")
            self._display_current()
            return
        if view.state == "kanban":
            board_id = view.context["board_id"]
            self.client.invalidate(f"board_config|{board_id}")
            try:
                config = self.client.get_board_config(board_id)
                issues = self.client.get_board_issues(board_id)
                columns, flat = self._build_kanban_columns(config, issues)
                view.data = columns
                view.context["flat_issues"] = flat
            except Exception as e:
                print(f"Error: {e}")
                return
            view.page = 0
            print("Refreshed.\n")
            self._display_current()
            return
        if view.state == "epics":
            ctx = view.context
            try:
                view.data = self.client.get_epics(
                    keyword=ctx.get("keyword"), **ctx.get("time_kwargs", {})
                )
            except Exception as e:
                print(f"Error: {e}")
                return
        elif view.state == "mine":
            try:
                view.data = self.client.get_my_issues(**view.context.get("time_kwargs", {}))
            except Exception as e:
                print(f"Error: {e}")
                return
        elif view.state == "tickets":
            try:
                view.data = self.client.get_tickets(view.context["epic_key"])
            except Exception as e:
                print(f"Error: {e}")
                return
        elif view.state == "ticket":
            try:
                view.data = self.client.get_ticket(view.context["ticket_key"])
            except Exception as e:
                print(f"Error: {e}")
                return
        view.page = 0
        print("Refreshed.\n")
        self._display_current()

    def cmd_help(self):
        state = self.nav_stack[-1].state
        print("""
  Commands:
    search <keyword> [last <N> days|weeks|months]
                          Search epics by keyword or project prefix
    list [last <N> days|weeks|months]
                          List all epics, optionally filtered by age
    mine [last <N> days|weeks|months]
                          List tickets assigned to you
    select <number>       Select an epic from the current list (or just type the number)
    tickets               Re-display tickets for the current epic
    view <number>         View full details of a ticket (or just type the number)
    next / prev           Next/previous page, or navigate between tickets
    create epic           Create a new epic (prompts for project, summary, description)
    create ticket         Create a ticket inside the current epic (Story/Task/Bug)
    transition            Move the current ticket to a new status (ticket view only)
    comment               Add a comment to the current ticket (ticket view only)
    boards [<project>]    List kanban boards, optionally filtered by project key
    board <number>        Open a kanban board (or just type the number in boards view)
    back                  Return to the previous view
    home                  Jump back to the top level in one step
    refresh               Re-fetch data for the current view
    help                  Show this help
    exit / quit           Exit the CLI

  Examples:
    boards                            List all kanban boards
    boards PROJ                       Boards for project PROJ
    board 1                           Open the first board in kanban view
    mine                              Show all tickets assigned to you
    mine last 2 weeks                 Your tickets updated in the last 2 weeks
    search ceph last 6 months         Ceph epics created in the last 6 months
    select 2                          Select epic #2 to see its tickets
    view 5                            View full details of ticket #5
    transition                        Move current ticket to a new status
    comment                           Add a comment to the current ticket
""")
        print(f"  Current context: {state}\n")

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _display_current(self):
        view = self.nav_stack[-1]
        if view.state == "home":
            print("  Type 'list' to see recent epics, 'mine' for your tickets, 'boards' for kanban boards, or 'search <keyword>' to find specific ones.")
        elif view.state == "epics":
            self._display_epics(view)
        elif view.state in ("tickets", "mine"):
            self._display_tickets(view)
        elif view.state == "ticket":
            self._display_ticket(view)
        elif view.state == "boards":
            self._display_boards(view)
        elif view.state == "kanban":
            self._display_kanban(view)

    def _display_epics(self, view: View):
        data = view.data
        if not data:
            print("  No epics found.")
            return

        total = len(data)
        start = view.page * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_data = data[start:end]
        max_page = max(0, (total - 1) // PAGE_SIZE)

        print(f"\n  {'#':<5} {'Key':<14} {'Status':<18} {'Created':<12} Summary")
        print("  " + "─" * 78)
        for i, epic in enumerate(page_data, start + 1):
            f = epic["fields"]
            key = epic["key"]
            summary = truncate(f.get("summary", ""), 40)
            status = truncate(f.get("status", {}).get("name", ""), 16)
            created = fmt_date(f.get("created", ""))
            print(f"  {i:<5} {key:<14} {color_status(status, 18)} {created:<12} {summary}")

        print("  " + "─" * 78)
        print(f"  {total} epic(s)  |  page {view.page + 1}/{max_page + 1}")
        if max_page > 0:
            print("  Use 'next'/'prev' to page, 'select <number>' to drill in.")
        else:
            print("  Type 'select <number>' (or just the number) to view tickets.")
        print()

    def _display_tickets(self, view: View):
        data = view.data
        epic_key = view.context.get("epic_key", "")
        epic_summary = view.context.get("epic_summary", "")

        print(f"\n  Epic: [{epic_key}] {epic_summary}")

        if not data:
            print("  No tickets found in this epic.")
            print()
            return

        total = len(data)
        start = view.page * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_data = data[start:end]
        max_page = max(0, (total - 1) // PAGE_SIZE)

        print(f"\n  {'#':<5} {'Key':<14} {'Status':<18} {'Priority':<12} Summary")
        print("  " + "─" * 78)
        for i, ticket in enumerate(page_data, start + 1):
            f = ticket["fields"]
            key = ticket["key"]
            summary = truncate(f.get("summary", ""), 38)
            status = truncate(f.get("status", {}).get("name", ""), 16)
            priority = f.get("priority", {}).get("name", "") if f.get("priority") else ""
            print(f"  {i:<5} {key:<14} {color_status(status, 18)} {color_priority(priority, 12)} {summary}")

        print("  " + "─" * 78)
        print(f"  {total} ticket(s)  |  page {view.page + 1}/{max_page + 1}")
        if max_page > 0:
            print("  Use 'next'/'prev' to page, 'view <number>' to see details.")
        else:
            print("  Type 'view <number>' (or just the number) to see full details.")
        print()

    def _display_boards(self, view: View):
        data = view.data
        if not data:
            print("  No kanban boards found. Try 'boards <project>' to filter by project.")
            return
        print(f"\n  {'#':<5} {'ID':<8} {'Type':<12} Name")
        print("  " + "─" * 60)
        for i, board in enumerate(data, 1):
            bid = str(board.get("id", ""))
            btype = board.get("type", "").capitalize()
            name = truncate(board.get("name", ""), 38)
            print(f"  {i:<5} {bid:<8} {btype:<12} {name}")
        print("  " + "─" * 60)
        print(f"  {len(data)} board(s)  |  Type 'board <number>' (or just the number) to open.\n")

    def _display_kanban(self, view: View):
        board_name = view.context.get("board_name", "")
        columns = view.data
        if not columns:
            print("  No columns found for this board.")
            return

        print(f"\n  Board: {board_name}\n")

        counter = 1
        max_per_col = 12
        for col in columns:
            issues = col["issues"]
            col_name = col["name"]
            count = len(issues)
            header = f"  ── {color_status(col_name)} "
            fill = "─" * max(0, 74 - len(col_name) - 4)
            print(f"{header}{fill}  ({count})")
            if not issues:
                print("  (empty)\n")
                continue
            shown = issues[:max_per_col]
            for issue in shown:
                f = issue["fields"]
                key = issue["key"]
                summary = truncate(f.get("summary", ""), 52)
                print(f"  {counter:<4} {key:<14} {summary}")
                counter += 1
            remaining = count - len(shown)
            if remaining > 0:
                print(f"       … {remaining} more (use 'refresh' to reload)")
            print()

        total = sum(len(c["issues"]) for c in columns)
        print(f"  {total} issue(s) total. Type 'view <number>' to open a ticket, 'back' to return.\n")

    def _display_ticket(self, view: View):
        data = view.data
        f = data["fields"]
        key = data["key"]

        w = 60
        print(f"\n  {'═' * w}")
        print(f"  {key}  —  {truncate(f.get('summary', ''), w - len(key) - 5)}")
        print(f"  {'═' * w}")

        status = f.get("status", {}).get("name", "")
        priority = f.get("priority", {}).get("name", "") if f.get("priority") else "None"
        assignee = fmt_user(f.get("assignee"))
        created = fmt_date(f.get("created", ""))
        updated = fmt_date(f.get("updated", ""))
        labels = ", ".join(f.get("labels", [])) or "—"
        itype = f.get("issuetype", {}).get("name", "")

        print(f"  Type     : {itype}")
        print(f"  Status   : {color_status(status)}")
        print(f"  Priority : {color_priority(priority)}")
        print(f"  Assignee : {assignee}")
        print(f"  Created  : {created}   Updated: {updated}")
        print(f"  Labels   : {labels}")

        desc = self.client.extract_text(f.get("description"))
        if desc:
            print(f"\n  Description:")
            print("  " + "─" * 56)
            for line in desc.strip().splitlines():
                print(f"    {line}")

        comments = f.get("comment", {}).get("comments", [])
        if comments:
            print(f"\n  Comments ({len(comments)} total, showing last 3):")
            print("  " + "─" * 56)
            for c in comments[-3:]:
                author = fmt_user(c.get("author"))
                date = fmt_date(c.get("created", ""))
                body = truncate(self.client.extract_text(c.get("body")), 300)
                print(f"\n  [{date}] {author}:")
                for line in body.strip().splitlines():
                    print(f"    {line}")

        print(f"\n  {'═' * w}")
        print("  Commands here: 'transition', 'comment', 'back', 'next'/'prev'.\n")

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------

    def _parent_view(self, state: str) -> "View | None":
        """Find the most recent view of the given state in the stack."""
        for v in reversed(self.nav_stack):
            if v.state == state:
                return v
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="jirashell", add_help=True)
    parser.add_argument(
        "--configure", action="store_true",
        help="Run the configuration wizard and exit",
    )
    args = parser.parse_args()

    try:
        if args.configure:
            run_configure(existing=load_config())
            sys.exit(0)

        creds = load_config()
        if creds is None:
            print("No configuration found.")
            creds = run_configure()
            print()

        domain, email, api_key = creds
        cli = JiraCLI(domain, email, api_key)
        cli.run()
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
