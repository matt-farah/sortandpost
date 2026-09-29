"""The packaged desktop app (V2 P5): a menu-bar / tray process that supervises
the web server.

Two processes from one executable:

* the **tray** (:mod:`sortandpost.desktop.tray`) -- single instance, picks a
  localhost port, spawns the server, opens the browser, owns the menu (open,
  pause / resume indexing, start at login, logs, quit);
* the **server** -- the ordinary web app (``sortandpost --serve``), bound to
  ``127.0.0.1``. It exposes a small token-gated control API
  (:mod:`sortandpost.desktop.control`) the tray talks to, and shuts itself down
  cleanly (final publish, lock release) if the tray goes away.

A source / Docker run (``python -m sortandpost``) is just the server, as before;
``python -m sortandpost --tray`` runs the desktop flow from source.
"""
