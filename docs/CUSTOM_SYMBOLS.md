# Custom symbols and text commands

Dictate 1.2.1 reads user mappings from an ordinary **UTF-8 `.txt` file**. No Python edits or recompilation are needed.

Open **Settings → Custom symbols (.txt)**:

- **Edit** creates/opens `%APPDATA%\Dictate\custom_symbols.txt` in your default text editor.
- **Select…** chooses a different `.txt` file. The selected path is saved with your preferences.
- **Reload** validates the saved file immediately. Saved edits also reload automatically when the next complete dictation is normalized.

## Format

```text
# A comment occupies its own line.
happy face = :)
work email = name@example.com
my company = Example & Co.
next paragraph = \n\n
my signature = "Best regards,\nYour name"
quoted text = "  keep these spaces  "
windows folder = C:\\Users\\YourName
enter = <enter>
```

The first `=` separates the spoken phrase from its literal replacement. A replacement may contain additional `=` characters. Blank lines and lines beginning with `#` are ignored; a `#` after the separator is literal text, not an inline comment.

- Matching is Unicode case-insensitive, with phrase/word boundaries. Longer matching phrases take precedence. Repeated definitions use the last value.
- Use `\n` for a newline, `\t` for a tab and `\\` for a backslash. CR/CRLF replacements become LF newlines.
- Surround a replacement with JSON double quotes to preserve leading/trailing spaces. JSON escapes such as `\"` are supported in quoted values.
- The replacement is **literal**: `my token = five zero` produces the words `five zero`, not `50`. Built-in symbol, number and Enter commands do not reinterpret replacement text.
- Existing literal URLs and email addresses are protected from command processing.
- Custom phrases can override built-in phrases, including `enter`. Otherwise the original 167 symbols, natural currencies and exact number conversion remain available.

Aliases are not regular expressions. The file cannot execute Python or shell commands. Limits: 4 MiB, 5,000 distinct phrases, 256 characters per phrase, and no NUL characters.

An invalid saved edit leaves the last successfully loaded mappings active **for the current app session** and reports an error. Correct the file and reload. On a fresh launch with an invalid file, built-in commands still work, but invalid custom mappings cannot be recovered from that file.

## Spoken Enter

Say:

> hello enter world enter five zero

Dictate inserts:

```text
hello
world
50
```

The command becomes a newline **inside the assembled text**. Speech continues on the next line; pauses still do not submit anything. The entire recording is pasted once when you press Stop.

Say **literal enter** to insert the word `enter`. You can also define your own `enter` mapping.

**This command never presses the Enter key.** The existing **Auto Enter** setting is separate: if enabled, it sends exactly one Enter after the complete paste. Disable Auto Enter when you do not want that final keypress. If multiline clipboard insertion is unavailable, Dictate reports an error rather than falling back to simulated Enter keystrokes.
