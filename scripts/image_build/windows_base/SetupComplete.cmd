@echo off
rem Installed by proxmox_base_windows.sh (finalize.ps1) as
rem %WINDIR%\Setup\Scripts\SetupComplete.cmd. Windows runs it once, as SYSTEM,
rem when setup on a clone of the base has finished, after the specialize
rem reboot. Only then does sshd start: an SSH session that the builder opened
rem earlier would be cut by that reboot, and a setup step could be lost.
sc.exe config sshd start= auto
sc.exe start sshd
rem The answer file and its copies (no secrets in them, but a lab image has no
rem use for them) and the specialize script.
del /f /q "%WINDIR%\System32\Sysprep\base-unattend.xml"
del /f /q "%WINDIR%\Panther\unattend.xml"
del /f /q "%WINDIR%\Panther\Unattend\unattend.xml"
del /f /q "%WINDIR%\Setup\Scripts\randomize-admin.ps1"
rem Runs once: an image built on this base must not re-enable sshd if it is
rem ever sysprepped again.
del /f /q "%~f0"
