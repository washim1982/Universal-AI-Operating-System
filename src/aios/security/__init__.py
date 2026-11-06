"""Security Kernel: identity, policy decisions, secrets and the audit log (WP 2.2).

:mod:`aios.security.identity` signs callers in (ADR-011). :mod:`aios.security.cedar` compiles Policy
manifests and decides requests (ADR-007), and :mod:`aios.security.enforcement` adds the kill switch
and delegation in front of it. :mod:`aios.security.audit` keeps the hash chain, and
:mod:`aios.security.secrets` serves tool credentials. :mod:`aios.security.postgres` stores it all.
"""
