use framework "Foundation"
use scripting additions

on run argv
    set requestData to current application's NSData's dataWithContentsOfFile:(item 1 of argv)
    set requestObject to current application's NSJSONSerialization's JSONObjectWithData:requestData options:0 |error|:(missing value)
    set senderText to (requestObject's objectForKey:"formatted_sender") as text
    set senderAddress to (requestObject's objectForKey:"sender") as text
    set sendingAccountID to (requestObject's objectForKey:"account_id") as text
    set subjectText to (requestObject's objectForKey:"subject") as text
    set bodyText to (requestObject's objectForKey:"body") as text
    set toAddresses to (requestObject's objectForKey:"to") as list
    set ccAddresses to (requestObject's objectForKey:"cc") as list
    set bccAddresses to (requestObject's objectForKey:"bcc") as list
    set attachmentPaths to (requestObject's objectForKey:"attach") as list
    set replySource to requestObject's objectForKey:"reply_source"
    set theStage to "compose"
    try
        tell application "Mail"
            set sendingAccount to first account whose id is sendingAccountID
            set allowedAddresses to get email addresses of sendingAccount
            if senderAddress is not in allowedAddresses then error "Sender is not an address on the selected Mail account"
            if replySource is missing value then
                set outgoing to make new outgoing message with properties {sender:senderText, subject:subjectText, visible:false}
            else
                set sourceAccountID to (replySource's objectForKey:"account") as text
                set sourceMailbox to (replySource's objectForKey:"mailbox") as text
                set sourceLeaf to (current application's NSString's stringWithString:sourceMailbox)'s lastPathComponent() as text
                set sourceID to (replySource's objectForKey:"id") as integer
                set expectedMessageID to (replySource's objectForKey:"message_id") as text
                set sourceAccount to first account whose id is sourceAccountID
                set originalMessage to missing value
                repeat with sourceBox in (mailboxes of sourceAccount whose name is sourceLeaf)
                    set matches to (messages of sourceBox whose id is sourceID)
                    if (count matches) > 0 then
                        set originalMessage to item 1 of matches
                        exit repeat
                    end if
                end repeat
                if originalMessage is missing value then error "Original reply message is unavailable"
                if my normalID(message id of originalMessage) is not my normalID(expectedMessageID) then error "Original reply message identity changed"
                set outgoing to reply originalMessage without opening window
                set sender of outgoing to senderText
                set subject of outgoing to subjectText
                delete every to recipient of outgoing
                delete every cc recipient of outgoing
                delete every bcc recipient of outgoing
            end if
            set content of outgoing to bodyText & linefeed & linefeed
            tell outgoing
                repeat with emailAddress in toAddresses
                    make new to recipient at end of to recipients with properties {address:(emailAddress as text)}
                end repeat
                repeat with emailAddress in ccAddresses
                    make new cc recipient at end of cc recipients with properties {address:(emailAddress as text)}
                end repeat
                repeat with emailAddress in bccAddresses
                    make new bcc recipient at end of bcc recipients with properties {address:(emailAddress as text)}
                end repeat
                repeat with attachmentPath in attachmentPaths
                    tell content
                        make new attachment with properties {file name:(POSIX file (attachmentPath as text))} at after last paragraph
                    end tell
                end repeat
            end tell
            set effectiveSender to sender of outgoing
            set senderIsAllowed to false
            repeat with allowedAddress in allowedAddresses
                if effectiveSender is (allowedAddress as text) or effectiveSender contains ("<" & (allowedAddress as text) & ">") then set senderIsAllowed to true
            end repeat
            if not senderIsAllowed then error "Mail selected an unexpected sender account"
            set theStage to "send"
            set successful to send outgoing
        end tell
        if successful then
            return "{\"mail_send_result\":true,\"engine\":\"applescript\"}"
        else
            return "{\"mail_send_result\":false,\"engine\":\"applescript\"}"
        end if
    on error errorText number errorNumber
        return "{\"stage\":\"" & theStage & "\",\"error_number\":" & errorNumber & "}"
    end try
end run

on normalID(value)
    set valueString to current application's NSString's stringWithString:value
    return (valueString's stringByTrimmingCharactersInSet:(current application's NSCharacterSet's characterSetWithCharactersInString:"<> ")) as text
end normalID
