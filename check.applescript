on run argv
    -- 1. 初始化变量
    set userPrompt to ""
    set inputSelector to ""
    set resultSelector to ""
    set submitSelector to ""
    set continueSelector to ""
    set pollInterval to 3
    set pollMax to 60

    -- 2. 手动解析 CLI 标志
    set i to 1
    repeat while i ≤ (count of argv)
        set currentArg to item i of argv

        if currentArg is "-p" or currentArg is "--prompt" then
            set i to i + 1
            set userPrompt to item i of argv
        else if currentArg is "-i" or currentArg is "--input" then
            set i to i + 1
            set inputSelector to item i of argv
        else if currentArg is "-r" or currentArg is "--result" then
            set i to i + 1
            set resultSelector to item i of argv
        else if currentArg is "-b" or currentArg is "--button" then
            set i to i + 1
            set submitSelector to item i of argv
        else if currentArg is "-c" or currentArg is "--continue-selector" then
            set i to i + 1
            set continueSelector to item i of argv
        else if currentArg is "--poll-interval" then
            set i to i + 1
            set pollInterval to (item i of argv) as integer
        else if currentArg is "--poll-max" then
            set i to i + 1
            set pollMax to (item i of argv) as integer
        else if currentArg is "-h" or currentArg is "--help" then
            return "用法: osascript check.applescript -p \"提示词\" -i \"输入框选择器\" -r \"结果选择器\" [-b \"发送按钮选择器\"] [-c \"继续按钮选择器\"] [--poll-interval 秒] [--poll-max 次数]"
        end if

        set i to i + 1
    end repeat

    -- 3. 必填参数校验
    if userPrompt is "" or inputSelector is "" or resultSelector is "" then
        return "ERR:" & return & "缺少必要参数。用法: osascript check.applescript -p \"提示词\" -i \"输入框选择器\" -r \"结果选择器\" [-b \"发送按钮选择器\"]"
    end if

    -- 4. 安全转义（负责 JS 字符串边界：反斜杠、引号、控制字符）
    set safePrompt to my replaceText(userPrompt, "\\", "\\\\")
    set safePrompt to my replaceText(safePrompt, "\"", "\\\"")
    set safePrompt to my replaceText(safePrompt, "'", "\\'")
    -- 控制字符必须在反斜杠翻倍之后转义，这样新增的 \n / \r / \t 不会被二次翻倍
    set safePrompt to my replaceText(safePrompt, linefeed, (character id 92) & "n")
    set safePrompt to my replaceText(safePrompt, (character id 13), (character id 92) & "r")
    set safePrompt to my replaceText(safePrompt, tab, (character id 92) & "t")

    -- 5. 静默注入并发送
    set jsInject to "(function() {" & ¬
        "var el = document.querySelector('" & inputSelector & "');" & ¬
        "if(!el) return 'ERROR_INPUT_NOT_FOUND';" & ¬
        "var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;" & ¬
        "nativeSetter.call(el, '" & safePrompt & "');" & ¬
        "el.dispatchEvent(new Event('input', {bubbles: true}));"

    if submitSelector is not "" then
        set jsInject to jsInject & ¬
            "var btn = document.querySelector('" & submitSelector & "');" & ¬
            "if(btn) { btn.click(); return 'SENT_BY_CLICK'; }" & ¬
            "else { return 'ERROR_BUTTON_NOT_FOUND'; }"
    else
        set jsInject to jsInject & ¬
            "el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));" & ¬
            "return 'SENT_BY_ENTER';"
    end if

    set jsInject to jsInject & "})();"

    tell application "Safari"
        try
            set injectResult to do JavaScript jsInject in current tab of window 1
        on error
            return "ERR:" & return & "注入失败：请确认 Safari 已经开启 '允许 Apple 事件中的 JavaScript'，且当前标签页是 DeepSeek。"
        end try
    end tell

    if injectResult contains "ERROR" then
        return "ERR:" & return & "注入失败: " & injectResult
    end if

    -- 6. 静默轮询抓取结果（并自动点击 DeepSeek 的「继续」按钮以取回完整回复）
    set lastText to ""
    set stableCount to 0
    set finalText to ""
    set continuePrefix to "__CONTINUE__"

    repeat pollMax times
        delay pollInterval

        set jsCommand to "(function(){" & ¬
            "var nodes = document.querySelectorAll('" & resultSelector & "');" & ¬
            "var text = nodes.length > 0 ? (nodes[nodes.length - 1].innerText || '') : '';" & ¬
            "var continued = false;" & ¬
            "var override = '" & continueSelector & "';" & ¬
            "var cands = override ? document.querySelectorAll(override) : document.querySelectorAll('button, [role=button]');" & ¬
            "for (var i = 0; i < cands.length; i++) {" & ¬
            "var el = cands[i];" & ¬
            "var t = (el.innerText || el.textContent || '').trim();" & ¬
            "var hit = override ? true : (t === '继续' || t === '继续生成' || t === '继续回答' || t.toLowerCase() === 'continue' || t.toLowerCase() === 'continue generating');" & ¬
            "if (hit) { try { el.click(); } catch (e) {} continued = true; break; }" & ¬
            "}" & ¬
            "return (continued ? '__CONTINUE__' : '') + text;" & ¬
            "})();"

        tell application "Safari"
            try
                set currentText to do JavaScript jsCommand in current tab of window 1
            on error
                set currentText to ""
            end try
        end tell

        if currentText begins with continuePrefix then
            set stableCount to 0
        else if currentText is not "" and currentText is equal to lastText then
            set stableCount to stableCount + 1
            if stableCount ≥ 3 then
                set finalText to currentText
                exit repeat
            end if
        else
            set stableCount to 0
        end if

        set lastText to currentText
    end repeat

    if finalText is "" then
        return "ERR:" & return & "超时或抓取失败，请检查结果选择器 '" & resultSelector & "' 是否正确"
    else
        return "OK:" & return & finalText
    end if
end run

on replaceText(theText, searchString, replacementString)
    set AppleScript's text item delimiters to searchString
    set theTextItems to every text item of theText
    set AppleScript's text item delimiters to replacementString
    set theText to theTextItems as string
    set AppleScript's text item delimiters to ""
    return theText
end replaceText
