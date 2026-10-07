// Server-Sent Events over fetch() (EventSource can't send the Authorization header).
// Follows the SSE format: frames end with a blank line; "field: value" lines; ":" lines are comments
// (heartbeats); multiple data lines are joined with "\n".

export interface SseFrame {
    event: string;
    data: string;
    id?: string;
}

export class SseParser {
    private buffer = "";

    push(chunk: string): SseFrame[] {
        this.buffer += chunk.replace(/\r\n?/g, "\n");
        const frames: SseFrame[] = [];
        let end = this.buffer.indexOf("\n\n");
        while (end !== -1) {
            const frame = parseFrame(this.buffer.slice(0, end));
            if (frame) frames.push(frame);
            this.buffer = this.buffer.slice(end + 2);
            end = this.buffer.indexOf("\n\n");
        }
        return frames;
    }
}

function parseFrame(block: string): SseFrame | null {
    let event = "message";
    let id: string | undefined;
    const data: string[] = [];
    for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
        else if (field === "id") id = value;
    }
    return data.length ? { event, data: data.join("\n"), id } : null;
}

export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    try {
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            for (const frame of parser.push(decoder.decode(value, { stream: true }))) yield frame;
        }
        for (const frame of parser.push(decoder.decode() + "\n\n")) yield frame;
    } finally {
        reader.releaseLock();
    }
}
