"""Security Kernel: identity, policy engine binding, secrets, audit and taint tracking (WP 2.2).

The policy engine is Cedar (ADR-007): :mod:`aios.security.cedar` compiles Policy manifests and
decides requests behind the :class:`aios.core.policy.PolicyDecisionPoint` port. It needs the
``policy`` extra.
"""
