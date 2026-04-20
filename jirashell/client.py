import time
import requests
from requests.auth import HTTPBasicAuth


class JiraClient:
    def __init__(self, domain, email, api_key):
        self.base_url = f"https://{domain}.atlassian.net/rest/api/3"
        self.auth = HTTPBasicAuth(email, api_key)
        self.headers = {"Accept": "application/json"}
        self._cache = {}  # key -> (timestamp, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        resp = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        resp.raise_for_status()
        return resp.json()

    def _cached(self, key, ttl, fn):
        now = time.time()
        if key in self._cache:
            ts, val = self._cache[key]
            if now - ts < ttl:
                return val
        val = fn()
        self._cache[key] = (now, val)
        return val

    def _jql(self, jql, fields, max_results=100):
        data = self._get("search/jql", params={
            "jql": jql,
            "maxResults": max_results,
            "fields": fields,
        })
        return data.get("issues", [])

    def _build_time_clause(self, days=None, weeks=None, months=None):
        if days:
            return f"created >= -{days}d"
        if weeks:
            return f"created >= -{weeks}w"
        if months:
            return f"created >= startOfMonth(-{months})"
        return None

    def extract_text(self, node):
        """Recursively extract plain text from Atlassian Document Format (ADF)."""
        if not node:
            return ""
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ""
        node_type = node.get("type", "")
        content = node.get("content", [])
        children = lambda: "\n".join(filter(None, (self.extract_text(c) for c in content)))

        if node_type == "text":
            return node.get("text", "")
        if node_type == "hardBreak":
            return "\n"
        if node_type == "mention":
            return node.get("attrs", {}).get("text", "")
        if node_type in ("inlineCard", "blockCard", "embedCard"):
            return node.get("attrs", {}).get("url", "")
        if node_type == "heading":
            return f"\n{children()}\n"
        if node_type == "codeBlock":
            return f"\n```\n{children()}\n```\n"
        # paragraph, bulletList, orderedList, listItem, blockquote, doc, etc.
        return children()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_epics(self, keyword=None, days=None, weeks=None, months=None):
        conditions = ["issuetype = Epic"]
        if keyword:
            conditions.append(
                f'(summary ~ "{keyword}" OR project = "{keyword.upper()}")'
            )
        time_clause = self._build_time_clause(days, weeks, months)
        if time_clause:
            conditions.append(time_clause)
        jql = " AND ".join(conditions) + " ORDER BY created DESC"

        cache_key = f"epics|{keyword}|{days}|{weeks}|{months}"
        return self._cached(cache_key, ttl=300, fn=lambda: self._jql(
            jql,
            fields="summary,created,status,assignee,project",
        ))

    def get_tickets(self, epic_key):
        cache_key = f"tickets|{epic_key}"

        def fetch():
            # Try new-style parent field first (Next-gen / Team-managed projects)
            results = self._jql(
                f"parent = {epic_key} ORDER BY created DESC",
                fields="summary,status,assignee,priority,issuetype,created,updated",
            )
            # Fall back to classic Epic Link custom field
            if not results:
                results = self._jql(
                    f'"Epic Link" = {epic_key} ORDER BY created DESC',
                    fields="summary,status,assignee,priority,issuetype,created,updated",
                )
            # If a project migrated, both may have results — union and dedupe
            elif results:
                legacy = self._jql(
                    f'"Epic Link" = {epic_key} ORDER BY created DESC',
                    fields="summary,status,assignee,priority,issuetype,created,updated",
                )
                seen = {i["key"]: i for i in results}
                for i in legacy:
                    seen.setdefault(i["key"], i)
                results = list(seen.values())
            return results

        return self._cached(cache_key, ttl=120, fn=fetch)

    def get_ticket(self, ticket_key):
        cache_key = f"ticket|{ticket_key}"
        return self._cached(cache_key, ttl=60, fn=lambda: self._get(
            f"issue/{ticket_key}",
            params={
                "fields": (
                    "summary,status,assignee,description,priority,"
                    "created,updated,labels,issuetype,comment,parent"
                )
            },
        ))

    def create_issue(self, project_key, summary, issue_type, description=None, epic_key=None):
        body = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": issue_type},
            }
        }
        if description:
            body["fields"]["description"] = self._text_to_adf(description)
        if epic_key:
            body["fields"]["parent"] = {"key": epic_key}

        headers = {**self.headers, "Content-Type": "application/json"}
        resp = requests.post(f"{self.base_url}/issue", headers=headers, auth=self.auth, json=body)

        # Classic projects use Epic Link custom field instead of parent
        if resp.status_code == 400 and epic_key:
            body["fields"].pop("parent", None)
            body["fields"]["customfield_10014"] = epic_key
            resp = requests.post(f"{self.base_url}/issue", headers=headers, auth=self.auth, json=body)

        resp.raise_for_status()
        return resp.json()

    def _text_to_adf(self, text):
        paragraphs = []
        for para in text.strip().split("\n\n"):
            para = para.strip()
            if para:
                paragraphs.append({
                    "type": "paragraph",
                    "content": [{"type": "text", "text": para}]
                })
        return {
            "type": "doc",
            "version": 1,
            "content": paragraphs or [{"type": "paragraph", "content": []}],
        }

    def get_my_issues(self, days=None, weeks=None, months=None):
        conditions = ["assignee = currentUser()"]
        time_clause = self._build_time_clause(days, weeks, months)
        if time_clause:
            conditions.append(time_clause)
        jql = " AND ".join(conditions) + " ORDER BY updated DESC"
        cache_key = f"mine|{days}|{weeks}|{months}"
        return self._cached(cache_key, ttl=120, fn=lambda: self._jql(
            jql,
            fields="summary,status,assignee,priority,issuetype,created,updated,parent",
            max_results=50,
        ))

    def get_transitions(self, ticket_key):
        data = self._get(f"issue/{ticket_key}/transitions")
        return data.get("transitions", [])

    def do_transition(self, ticket_key, transition_id):
        import json
        body = {"transition": {"id": transition_id}}
        headers = {**self.headers, "Content-Type": "application/json"}
        resp = requests.post(
            f"{self.base_url}/issue/{ticket_key}/transitions",
            headers=headers, auth=self.auth, json=body,
        )
        resp.raise_for_status()

    def add_comment(self, ticket_key, text):
        body = {"body": self._text_to_adf(text)}
        headers = {**self.headers, "Content-Type": "application/json"}
        resp = requests.post(
            f"{self.base_url}/issue/{ticket_key}/comment",
            headers=headers, auth=self.auth, json=body,
        )
        resp.raise_for_status()
        return resp.json()

    def invalidate(self, key=None):
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()
