on run
    do shell script "/usr/bin/open -n -a " & quoted form of "/Applications/KiCad/KiCad.app"
end run

on open projectFiles
    repeat with projectFile in projectFiles
        do shell script "/usr/bin/open -n -a " & quoted form of "/Applications/KiCad/KiCad.app" & " " & quoted form of (POSIX path of projectFile)
    end repeat
end open
