"""Telegram side of the Sherwood group-chat fund.

Modules:
    links       signed /tg/* deep links (pure)
    settings    env / Hermes secret access (pure)
    types       view dataclasses + the FundGateway port (pure)
    flow        pitch -> riff -> DraftRequested (pure)
    cards       (text_html, InlineKeyboardMarkup) renderers (needs PTB)
    cards_live  post / in-place edit / 60 s refresh (needs PTB)
    runtime     state shared with plugin-core (imports PTB lazily)
    handler     PTB handlers + wire() for Hermes' platform-handler factory (needs PTB)

Nothing here imports python-telegram-bot at package import time, so the plugin
registers even when PTB is missing; Hermes calls the factory (which imports
PTB) only once its Telegram adapter is up.
"""
