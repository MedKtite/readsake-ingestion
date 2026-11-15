"""Readsake ingestion — offline LibriVox audiobook pipeline.

Downloads volunteer-read recordings, trims the spoken LibriVox boilerplate
(intro/outro), loudness-normalizes, uploads the cleaned MP3s, and records
everything (including an audit of each cut) in the same PostgreSQL database
the Spring Boot backend uses. Not a live service — run manually or via cron.
"""
