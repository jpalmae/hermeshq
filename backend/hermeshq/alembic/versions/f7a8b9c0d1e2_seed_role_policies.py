"""Merge migration heads + seed role-based permission policies (office, technical, security)

Revision ID: a1b2c3d4e5f6
Revises: w7x8y9z0a1b2
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = ("w7x8y9z0a1b2", "b1c2d3e4f5a6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO permission_policies
          (id, name, description, tool_rules, path_rules, command_rules, network_rules, approval_rules, is_system, created_at, updated_at)
        VALUES
          (
            'sys-role-office',
            'Role: No técnico (Oficina)',
            'Perfil laboral de oficina — el agente conversa, redacta y resume. Sin shell, sin ejecución, sin red: no puede escanear ni atacar nada por diseño (no tiene herramientas para ello).',
            '{"allow": [], "deny": ["bash", "shell", "terminal", "edit", "write", "file", "zsh", "powershell", "cmd"]}'::json,
            '{"allow_paths": [], "deny_paths": ["/etc/**", "/root/**", "**/.ssh/**", "**/.aws/**", "**/id_rsa*"]}'::json,
            '{"allow": [], "deny": []}'::json,
            '{"allow_domains": [], "deny_all": true}'::json,
            '{"require_approval_for": [], "auto_approve_threshold": "none"}'::json,
            true,
            now(),
            now()
          ),
          (
            'sys-role-technical',
            'Role: Técnico / Dev / TI',
            'Perfil técnico — shell y archivos permitidos; bloquea escaneo de red, herramientas de pentesting y operaciones destructivas de disco.',
            '{"allow": [], "deny": []}'::json,
            '{"allow_paths": [], "deny_paths": ["**/.ssh/**", "**/.aws/**", "**/id_rsa*", "**/.gnupg/**"]}'::json,
            '{"allow": [], "deny": ["nmap *", "nmap*", "masscan*", "zmap*", "rustscan*", "naabu*", "netdiscover*", "arp-scan*", "arp_scan*", "tcpdump*", "tshark*", "wireshark*", "ettercap*", "bettercap*", "hydra*", "medusa*", "sqlmap*", "nikto*", "gobuster*", "dirb*", "ffuf*", "wpscan*", "aircrack*", "hashcat*", "john *", "responder*", "crackmapexec*", "nxc *", "enum4linux*", "impacket-*", "msfconsole*", "metasploit*", "msfvenom*", "searchsploit*", "shodan *", "amass*", "sublist3r*", "theHarvester*", "dd if=*", "mkfs*", "wipefs*", "shred /dev/*", "rm -rf /*", "rm -rf /", "chmod -R 777 /"]}'::json,
            '{"allow_domains": [], "deny_all": false}'::json,
            '{"require_approval_for": [], "auto_approve_threshold": "none"}'::json,
            true,
            now(),
            now()
          ),
          (
            'sys-role-security',
            'Role: Ciberseguridad',
            'Perfil de seguridad — escaneo y pentesting permitidos (es su trabajo); protege secretos locales y operaciones destructivas de disco.',
            '{"allow": [], "deny": []}'::json,
            '{"allow_paths": [], "deny_paths": ["**/.ssh/**", "**/.aws/**", "**/id_rsa*", "**/.gnupg/**", "**/.gnupg"]}'::json,
            '{"allow": [], "deny": ["dd if=/dev/*", "dd if=/dev/* of=*", "mkfs*", "wipefs*", "shred /dev/*", "rm -rf /*", "rm -rf /"]}'::json,
            '{"allow_domains": [], "deny_all": false}'::json,
            '{"require_approval_for": [], "auto_approve_threshold": "none"}'::json,
            true,
            now(),
            now()
          )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM permission_policies WHERE id IN ('sys-role-office', 'sys-role-technical', 'sys-role-security')")
