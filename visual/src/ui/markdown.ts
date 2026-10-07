// A deliberately tiny markdown subset, parsed to data (never to HTML):
// paragraphs, "- " / "* " bullet lists, **bold**, _italic_ / *italic*.
// Rendering builds React elements from this AST, so text can never become markup.

export type Inline = { kind: "text" | "bold" | "italic"; text: string };
export type Block = { kind: "p"; inlines: Inline[] } | { kind: "ul"; items: Inline[][] };

const INLINE = /\*\*(.+?)\*\*|_(.+?)_|\*(.+?)\*/g;

export function parseInline(text: string): Inline[] {
    const out: Inline[] = [];
    let last = 0;
    for (const match of text.matchAll(INLINE)) {
        const index = match.index ?? 0;
        if (index > last) out.push({ kind: "text", text: text.slice(last, index) });
        if (match[1] !== undefined) out.push({ kind: "bold", text: match[1] });
        else out.push({ kind: "italic", text: match[2] ?? match[3] ?? "" });
        last = index + match[0].length;
    }
    if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
    return out;
}

export function parseMarkdown(source: string): Block[] {
    const blocks: Block[] = [];
    let paragraph: string[] = [];
    let list: Inline[][] | null = null;

    const flushParagraph = (): void => {
        if (paragraph.length) blocks.push({ kind: "p", inlines: parseInline(paragraph.join(" ")) });
        paragraph = [];
    };
    const flushList = (): void => {
        if (list) blocks.push({ kind: "ul", items: list });
        list = null;
    };

    for (const raw of source.replace(/\r\n?/g, "\n").split("\n")) {
        const line = raw.trim();
        const bullet = /^[-*]\s+(.*)$/.exec(line);
        if (bullet) {
            flushParagraph();
            (list ??= []).push(parseInline(bullet[1]));
        } else if (!line) {
            flushParagraph();
            flushList();
        } else {
            flushList();
            paragraph.push(line);
        }
    }
    flushParagraph();
    flushList();
    return blocks;
}
