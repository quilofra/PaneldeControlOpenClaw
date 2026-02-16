"""Permissions manager for allowed commands and sudo usage.

This module encapsulates the logic for determining whether a given shell
command may be executed by OpenClaw and whether sudo privileges are
granted.  The allowlist is based on command prefixes: if a command
starts with one of the allowed prefixes it is permitted.  Users may
add or remove prefixes at runtime and toggle the sudo flag; changes
are persisted back to the configuration file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List


class Permissions:
    """Manage allowed commands and sudo settings.

    This class manages a mapping of commands to permission rules.  A
    rule may specify allowed subcommands and/or regular expression
    patterns for arguments.  If no subcommands or patterns are
    specified for a command, any invocation of that command (with any
    arguments) is permitted.

    Parameters
    ----------
    config_path : str
        Path to the JSON configuration file that includes
        ``allowed_commands`` (list of strings or dicts) and
        ``allow_sudo``.  Updates to the permissions are persisted back
        to this file.
    """

    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self._load()

    def _load(self) -> None:
        """Load allowlist and sudo flag from the configuration file.

        The ``allowed_commands`` entry may contain strings (simple
        command names) or dictionaries with keys ``command``,
        ``subcommands`` and ``args_patterns``.  ``subcommands``
        specifies which subcommands are permitted, and
        ``args_patterns`` is a list of regular expression patterns
        matched against the arguments string.  When ``args_patterns``
        is present, a command is allowed only if at least one pattern
        matches the arguments.  If both ``subcommands`` and
        ``args_patterns`` are omitted or empty, any invocation of the
        command is allowed.
        """
        self.allowlist = {}
        self.allow_sudo = False
        if not self.config_path.exists():
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        raw_cmds = cfg.get("allowed_commands", [])
        # Build structured allowlist
        for entry in raw_cmds:
            if isinstance(entry, dict):
                cmd = entry.get("command")
                if not cmd:
                    continue
                subcmds = entry.get("subcommands")
                patterns = entry.get("args_patterns")
                # Normalize to lists or None
                subcmd_list = list(subcmds) if subcmds else None
                pattern_list = list(patterns) if patterns else None
                self.allowlist[cmd] = {
                    "subcommands": subcmd_list,
                    "args_patterns": pattern_list,
                }
            elif isinstance(entry, str):
                # Simple allow all for this command
                self.allowlist[entry] = {
                    "subcommands": None,
                    "args_patterns": None,
                }
        self.allow_sudo = bool(cfg.get("allow_sudo", False))

    def _save(self) -> None:
        """Persist the current allowlist and sudo flag back to the config.

        Commands are saved either as simple strings (if they allow all
        subcommands and arguments) or as dictionaries with explicit
        ``subcommands`` and/or ``args_patterns`` keys.  The ordering
        of entries is not preserved.
        """
        if not self.config_path.exists():
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        entries = []
        for cmd, rules in self.allowlist.items():
            if rules is None:
                entries.append(cmd)
                continue
            subs = rules.get("subcommands")
            patterns = rules.get("args_patterns")
            if not subs and not patterns:
                entries.append(cmd)
            else:
                entry: dict = {"command": cmd}
                if subs:
                    entry["subcommands"] = list(subs)
                if patterns:
                    entry["args_patterns"] = list(patterns)
                entries.append(entry)
        cfg["allowed_commands"] = entries
        cfg["allow_sudo"] = self.allow_sudo
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def is_sudo_allowed(self) -> bool:
        """Return whether sudo usage is permitted."""
        return self.allow_sudo

    def set_sudo(self, allowed: bool) -> None:
        """Enable or disable sudo permission and persist the change."""
        self.allow_sudo = bool(allowed)
        self._save()

    def is_command_allowed(self, command: str) -> bool:
        """Return True if the given command invocation is allowed.

        Commands are matched by their base executable name (the first
        word or trailing segment after a slash).  If a rule defines
        subcommands, only those subcommands are permitted.  If a rule
        defines argument patterns, at least one pattern must match
        against the argument string (excluding the command itself).  If
        both subcommands and patterns are omitted, any invocation of
        the command is allowed.

        Parameters
        ----------
        command : str
            The entire shell command line, including arguments.

        Returns
        -------
        bool
            True if the invocation is permitted; False otherwise.
        """
        parts = command.strip().split()
        if not parts:
            return False
        cmd = parts[0]
        # Extract program name (in case of absolute path)
        base_cmd = cmd.split("/")[-1]
        subcmd = parts[1] if len(parts) > 1 else None
        args = parts[1:] if len(parts) > 1 else []
        # Join args into a single string for pattern matching
        args_str = " ".join(args)
        for allowed_cmd, rules in self.allowlist.items():
            # If allowlist value is None, allow all
            if rules is None:
                if base_cmd == allowed_cmd or cmd.endswith("/" + allowed_cmd):
                    return True
                continue
            # Check command match
            if base_cmd == allowed_cmd or cmd.endswith("/" + allowed_cmd):
                subcmds = rules.get("subcommands")
                patterns = rules.get("args_patterns")
                # Check subcommand restriction
                if subcmds:
                    if not subcmd or subcmd not in subcmds:
                        # Subcommand missing or not allowed
                        continue
                # Check argument patterns
                if patterns:
                    import re
                    matched = False
                    for pat in patterns:
                        try:
                            if re.search(pat, args_str):
                                matched = True
                                break
                        except re.error:
                            # Invalid regex pattern: ignore pattern
                            continue
                    if not matched:
                        # No patterns matched
                        continue
                # If we reach here, command passes restrictions
                return True
        return False

    def add_command(self, prefix: str) -> None:
        """Add a command prefix to the allowlist and persist.

        When adding a command without specifying subcommands or
        patterns, the rule will allow all invocations of that
        command.
        """
        if prefix not in self.allowlist:
            self.allowlist[prefix] = {"subcommands": None, "args_patterns": None}
            self._save()

    def remove_command(self, prefix: str) -> None:
        """Remove a command prefix from the allowlist and persist."""
        if prefix in self.allowlist:
            self.allowlist.pop(prefix, None)
            self._save()

    def get_allowlist(self) -> List[str]:
        """Return the current list of allowed command prefixes."""
        return list(self.allowlist.keys())
