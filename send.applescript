use framework "Foundation"
use scripting additions

on run argv
    set requestData to current application's NSData's dataWithContentsOfFile:(item 1 of argv)
    set q to current application's NSJSONSerialization's JSONObjectWithData:requestData options:0 |error|:(missing value)
    set operation to (q's objectForKey:"operation") as text
    set senderText to (q's objectForKey:"formatted_sender") as text
    set senderAddress to (q's objectForKey:"sender") as text
    set accountID to (q's objectForKey:"account_id") as text
    set subjectText to (q's objectForKey:"subject") as text
    set marker to (q's objectForKey:"compose_title") as text
    set toAddresses to (q's objectForKey:"to") as list
    set ccAddresses to (q's objectForKey:"cc") as list
    set bccAddresses to (q's objectForKey:"bcc") as list
    set theStage to "compose"
    try
        tell application "Mail"
            set sendingAccount to first account whose id is accountID
            if senderAddress is not in (email addresses of sendingAccount) then error "Sender is not configured on the selected account"
            if operation is "compose" then
                set replySource to q's objectForKey:"reply_source"
                if replySource is missing value then
                    set outgoing to make new outgoing message with properties {sender:senderText, subject:marker, visible:true}
                else
                    set sourceAccount to first account whose id is ((replySource's objectForKey:"account") as text)
                    set sourcePath to (replySource's objectForKey:"mailbox") as text
                    set sourceLeaf to (current application's NSString's stringWithString:sourcePath)'s lastPathComponent() as text
                    set sourceID to (replySource's objectForKey:"id") as integer
                    set expectedID to (replySource's objectForKey:"message_id") as text
                    set originalMessage to missing value
                    repeat with sourceBox in (mailboxes of sourceAccount whose name is sourceLeaf)
                        set candidates to messages of sourceBox whose id is sourceID
                        if (count candidates) is 1 then
                            if my normalID(message id of item 1 of candidates) is my normalID(expectedID) then set originalMessage to item 1 of candidates
                        end if
                    end repeat
                    if originalMessage is missing value then error "The original reply message is unavailable or changed"
                    set outgoing to reply originalMessage with opening window
                    set sender of outgoing to senderText
                    set subject of outgoing to marker
                    delete every to recipient of outgoing
                    delete every cc recipient of outgoing
                    delete every bcc recipient of outgoing
                end if
                return "{\"outgoing_id\":" & (id of outgoing) & ",\"stage\":\"composed\"}"
            end if
            set outgoing to outgoing message id ((q's objectForKey:"outgoing_id") as integer)
            if operation is "prepare" then
                set theStage to "prepare-recipients"
                if subject of outgoing is not marker then error "Compose identity changed"
                tell outgoing
                    repeat with addr in toAddresses
                        make new to recipient at end of to recipients with properties {address:(addr as text)}
                    end repeat
                    repeat with addr in ccAddresses
                        make new cc recipient at end of cc recipients with properties {address:(addr as text)}
                    end repeat
                    repeat with addr in bccAddresses
                        make new bcc recipient at end of bcc recipients with properties {address:(addr as text)}
                    end repeat
                end tell
                set theStage to "prepare-save"
                set subject of outgoing to subjectText
                save outgoing
                return "{\"stage\":\"prepared\"}"
            end if
            if operation is not "submit" then error "Unknown compose operation"
            if subject of outgoing is not subjectText then error "Subject changed after preparation"
            set effectiveSender to sender of outgoing
            if effectiveSender is not senderAddress and effectiveSender does not contain ("<" & senderAddress & ">") then error "Sender changed after preparation"
            if (address of every to recipient of outgoing) is not toAddresses then error "To recipients changed after preparation"
            if (address of every cc recipient of outgoing) is not ccAddresses then error "Cc recipients changed after preparation"
            if (address of every bcc recipient of outgoing) is not bccAddresses then error "Bcc recipients changed after preparation"
            set theStage to "send"
            set successful to send outgoing
            if successful then return "{\"mail_send_result\":true,\"engine\":\"mail_editor\"}"
            return "{\"mail_send_result\":false,\"engine\":\"mail_editor\"}"
        end tell
    on error errorText number errorNumber
        return "{\"stage\":\"" & theStage & "\",\"error_number\":" & errorNumber & "}"
    end try
end run

on normalID(value)
    set valueString to current application's NSString's stringWithString:value
    return (valueString's stringByTrimmingCharactersInSet:(current application's NSCharacterSet's characterSetWithCharactersInString:"<> ")) as text
end normalID
