"""Fansly Manager — dashboard de gestion multi-instance.

Container separe (port 8500) qui pilote les containers fansly-bot-NAME
via le socket Docker monte en bind. UI Streamlit avec :
  - sélecteur de compte courant (sidebar)
  - vue d'ensemble cross-comptes (status, port, dashboard)
  - iframe vers le dashboard de l'instance selectionnee
"""
