use framework "AppKit"
use framework "Foundation"
use framework "UniformTypeIdentifiers"
use scripting additions

on run arguments
    set operation to item 1 of arguments
    set contentType to current application's UTType's typeWithFilenameExtension:"kicad_pro"
    set workspace to current application's NSWorkspace's sharedWorkspace()

    if operation is "get" then
        set applicationURL to workspace's URLForApplicationToOpenContentType:contentType
        if applicationURL is missing value then
            return "none"
        end if
        return applicationURL's |path|() as text
    end if

    if operation is "set" then
        set applicationPath to item 2 of arguments
        set applicationURL to current application's NSURL's fileURLWithPath:applicationPath
        workspace's setDefaultApplicationAtURL:applicationURL toOpenContentType:contentType completionHandler:(missing value)
        delay 1
        return
    end if

    error "Unknown operation: " & operation
end run
