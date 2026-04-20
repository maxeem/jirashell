# jirashell

An interactive Jira CLI for navigating epics, tickets, transitions, and comments without leaving your terminal.

## Installation

```bash
pip install jirashell
```

## Configuration

Run the setup wizard on first launch, or any time you need to update credentials:

```bash
jirashell --configure
```

You will be prompted for:
- **Jira subdomain** — e.g. `acme` for `acme.atlassian.net`
- **Atlassian email** — your login email
- **API token** — create one at [id.atlassian.com → Security → API tokens](https://id.atlassian.com/manage-profile/security/api-tokens)

Credentials are saved to `~/.jira` with permissions `0600`.

## Usage

```bash
jirashell
```

### Navigation

| Command | Description |
|---|---|
| `list [last N days\|weeks\|months]` | List all epics, optionally filtered by age |
| `search <keyword> [last N ...]` | Search epics by keyword or project prefix |
| `mine [last N days\|weeks\|months]` | Show tickets assigned to you |
| `select <number>` | Drill into an epic (or just type the number) |
| `tickets` | Re-display the current epic's tickets |
| `view <number>` | View full ticket details (or just type the number) |
| `next` / `prev` | Page through lists or move between tickets |
| `back` | Return to the previous view |
| `home` | Jump back to the top level |
| `refresh` | Re-fetch data for the current view |

### Creating issues

| Command | Description |
|---|---|
| `create epic` | Create a new epic (prompts for project, summary, description) |
| `create ticket` | Create a ticket inside the current epic (Story / Task / Bug) |

### Working with tickets

These commands are available when viewing a ticket:

| Command | Description |
|---|---|
| `transition` | Move the ticket to a new status |
| `comment` | Add a comment to the ticket |

### Examples

```
jirashell
home $ list last 3 months
home $ search payments
home > epics:payments $ select 2
home > epics:payments > PAY-42 $ view 5
home > epics:payments > PAY-42 > PAY-107 $ transition
home > epics:payments > PAY-42 > PAY-107 $ comment
home > epics:payments > PAY-42 > PAY-107 $ back
home $ mine last 2 weeks
```

## Requirements

- Python 3.10+
- A Jira Cloud account with API access
