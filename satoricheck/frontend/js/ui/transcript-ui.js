/**
 * Transcript UI Module
 * Handles Live Pro text streaming
 */

class TranscriptUI {
    constructor() {
        this.transcriptContainer = document.getElementById('transcript-container');
        this.micBtn = document.getElementById('mic-btn');
        this.transcriptContent = '';
    }

    appendTranscript(text, isFinal = false) {
        const existingPlaceholder = this.transcriptContainer.querySelector('.transcript-placeholder');
        if (existingPlaceholder) {
            existingPlaceholder.remove();
        }

        const transcriptText = this.transcriptContainer.querySelector('.transcript-text')
            || this.createTranscriptElement();

        if (isFinal) {
            // Remove all interim spans before adding final text
            const interimSpans = transcriptText.querySelectorAll('.interim');
            interimSpans.forEach(span => span.remove());

            transcriptText.textContent += text + ' ';
            this.transcriptContent = transcriptText.textContent;
        } else {
            // Remove previous interim results before adding new one
            const interimSpans = transcriptText.querySelectorAll('.interim');
            interimSpans.forEach(span => span.remove());

            const tempSpan = document.createElement('span');
            tempSpan.className = 'interim';
            tempSpan.textContent = text;
            transcriptText.appendChild(tempSpan);
        }

        // Auto-scroll
        this.transcriptContainer.scrollTop = this.transcriptContainer.scrollHeight;
    }

    createTranscriptElement() {
        const div = document.createElement('div');
        div.className = 'transcript-text';
        this.transcriptContainer.appendChild(div);
        return div;
    }

    setListeningState(isListening) {
        if (isListening) {
            this.micBtn.classList.add('active');
        } else {
            this.micBtn.classList.remove('active');
        }
    }
}

export default new TranscriptUI();
