@echo off
rem ailimclock launcher - double-click to start the widget.
rem A console may flash for a moment; use ailimclock.vbs for zero flash.
cd /d "%~dp0"
start "" pythonw "%~dp0ailimclock_widget.py"
