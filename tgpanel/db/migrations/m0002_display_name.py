"""Display name of a user: free-form single-line Unicode label, separate from the profile name."""

SQL = """
ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT '';
"""
