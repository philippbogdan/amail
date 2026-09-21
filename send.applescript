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
    set composed to missing value
    try
        tell application "Mail"
            set sendingAccount to first account whose id is accountID
            if senderAddress is not in (email addresses of sendingAccount) then error "Sender is not configured on the selected account"
            if operation is "compose" then
                set replySource to q's objectForKey:"reply_source"
                if replySource is missing value then set replySource to q's objectForKey:"forward_source"
                if replySource is missing value then
                    set outgoing to make new outgoing message with properties {sender:senderText, subject:marker, visible:true}
                    set composed to outgoing
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
                    if originalMessage is missing value then error "The original message is unavailable or changed"
                    -- Mail builds the quoted history, attribution line and thread headers itself.
                    if (replySource's objectForKey:"kind") as text is "forward" then
                        set outgoing to forward originalMessage with opening window
                    else
                        set outgoing to reply originalMessage with opening window
                    end if
                    set composed to outgoing
                    set sender of outgoing to senderText
                    set subject of outgoing to marker
                    delete every to recipient of outgoing
                    delete every cc recipient of outgoing
                    delete every bcc recipient of outgoing
                end if
                return "{\"outgoing_id\":" & (id of outgoing) & ",\"stage\":\"composed\"}"
            end if
            set outgoing to outgoing message id ((q's objectForKey:"outgoing_id") as integer)
            if operation is "discard" then
                -- Close amail's own compose window without saving, then remove the
                -- copy Mail may have autosaved into Drafts meanwhile. Only drafts
                -- of this account with this subject that did not exist before
                -- composition began are touched.
                delete outgoing
                set knownDrafts to (q's objectForKey:"known_drafts") as list
                set swept to 0
                set quietPasses to 0
                -- Mail files the autosaved copy a few seconds after the window closes.
                repeat 20 times
                    set found to 0
                    repeat with candidate in (messages of drafts mailbox whose subject is subjectText)
                        if (id of candidate) is not in knownDrafts and (id of account of mailbox of candidate) is accountID then
                            delete candidate
                            set swept to swept + 1
                            set found to found + 1
                        end if
                    end repeat
                    if found is 0 then
                        set quietPasses to quietPasses + 1
                    else
                        set quietPasses to 0
                    end if
                    if swept > 0 and quietPasses ≥ 2 then exit repeat
                    delay 0.5
                end repeat
                return "{\"stage\":\"discarded\",\"swept_drafts\":" & swept & "}"
            end if
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
        if composed is not missing value then
            -- Never leave a half-composed window behind when composition itself failed.
            try
                tell application "Mail" to delete composed
            end try
        end if
        return "{\"stage\":\"" & theStage & "\",\"error_number\":" & errorNumber & ",\"error_text\":\"" & my jsonText(errorText) & "\"}"
    end try
end run

on normalID(value)
    set valueString to current application's NSString's stringWithString:value
    return (valueString's stringByTrimmingCharactersInSet:(current application's NSCharacterSet's characterSetWithCharactersInString:"<> ")) as text
end normalID

on jsonText(value)
    set valueString to current application's NSString's stringWithString:(value as text)
    set valueString to valueString's stringByReplacingOccurrencesOfString:"\\" withString:"\\\\"
    set valueString to valueString's stringByReplacingOccurrencesOfString:"\"" withString:"\\\""
    set valueString to valueString's stringByReplacingOccurrencesOfString:linefeed withString:" "
    set valueString to valueString's stringByReplacingOccurrencesOfString:return withString:" "
    return valueString as text
end jsonText
