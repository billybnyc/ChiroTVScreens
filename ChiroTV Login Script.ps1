$env:CHIROTV_USER="Nettie"; $env:CHIROTV_PASS="lpcstaff"
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/billybnyc/ChiroTVScreens/main/bootstrap.ps1))) -EnableAutoLogon