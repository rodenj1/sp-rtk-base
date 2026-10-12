"""The Portal agent: a separate process, with its own systemd unit, that
will connect a Claimed base to the Portal (spec #82).

Named ``portal_agent`` to keep it apart from the setup-AP captive portal
(:class:`sp_rtk_base.services.net_provision.portal.Portal`), which is
unrelated.

Until a base is Claimed the agent is idle: it holds no base key, contacts
nothing and only answers status questions on its control socket
(:mod:`.control`). The app reads it through :mod:`.client`.
"""
