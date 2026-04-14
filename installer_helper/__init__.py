"""
Installer helper executable.

This is a small standalone binary (built via PyInstaller) that is invoked
by OS-native installers (MSI on Windows, PKG on macOS).

It provides a simple hooks-based system where developers define functions
in `installer_hooks.py` that get called during installation.

Available hooks:
- pre_install(install_root, data_root, is_upgrade) - Before installation
- post_install(install_root, data_root, is_upgrade) - After installation
"""
