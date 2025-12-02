# -*- coding: utf-8 -*-
"""
Update Checker (No-Op)
- Tetap diekspos agar import tidak error.
- Bisa di-override via env (MYXL_ENABLE_UPDATE=1) bila nanti ingin diaktifkan.
"""

import os

OWNER = "a"
REPO = "a"
BRANCH = "main"

def get_local_commit():
    return None

def get_latest_commit_atom():
    return None

def check_for_updates():
    """
    Return False supaya program utama lanjut normal.
    Set MYXL_ENABLE_UPDATE=1 jika suatu saat ingin mengaktifkan implementasi baru.
    """
    return False if os.getenv("MYXL_ENABLE_UPDATE", "0") != "1" else False