/**
 * Audio Manager Module
 * Handles Web Speech Recognition for live transcription
 */

import ui from './ui.js';

class AudioManager {
    constructor() {
        this.recognition = null;
        this.isListening = false;
        this.onResultCallback = null;
    }

    init() {
        // Check browser support
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

        if (!SpeechRecognition) {
            ui.showToast('Speech recognition not supported in this browser', 'error');
            return false;
        }

        // Set up recognition
        this.recognition = new SpeechRecognition();
        this.recognition.continuous = true;
        this.recognition.interimResults = true;
        this.recognition.lang = 'en-US';

        // Event handlers
        this.recognition.onresult = (event) => {
            this.handleResult(event);
        };

        this.recognition.onerror = (event) => {
            console.error('Speech recognition error:', event.error);

            if (event.error === 'not-allowed') {
                ui.showToast('Microphone permission denied', 'error');
            } else if (event.error === 'aborted') {
                // User manually stopped - show friendly message
                ui.showToast('Microphone stopped', 'info');
            } else if (event.error === 'no-speech') {
                // Debounce restart to prevent rapid-fire loop when mic is muted
                if (this.isListening && !this._restartPending) {
                    this._restartPending = true;
                    setTimeout(() => {
                        this._restartPending = false;
                        if (this.isListening) {
                            this.recognition.start();
                        }
                    }, 500);
                }
            } else {
                ui.showToast('Recognition error: ' + event.error, 'error');
            }

            this.isListening = false;
            ui.setListeningState(false);
        };

        this.recognition.onend = () => {
            // Restart if still supposed to be listening
            if (this.isListening) {
                this.recognition.start();
            }
        };

        return true;
    }

    start() {
        if (!this.recognition) {
            ui.showToast('Speech recognition not initialized', 'error');
            return;
        }

        if (this.isListening) {
            this.stop();
            return;
        }

        try {
            this.recognition.start();
            this.isListening = true;
            ui.setListeningState(true);
            ui.showToast('Listening...', 'info');
        } catch (error) {
            console.error('Error starting recognition:', error);
            ui.showToast('Failed to start listening', 'error');
        }
    }

    stop() {
        if (!this.recognition) return;

        this.isListening = false;
        ui.setListeningState(false);

        try {
            this.recognition.stop();
        } catch (error) {
            console.error('Error stopping recognition:', error);
        }
    }

    handleResult(event) {
        let interimTranscript = '';
        let finalTranscript = '';

        for (let i = event.resultIndex; i < event.results.length; i++) {
            const transcript = event.results[i][0].transcript;

            if (event.results[i].isFinal) {
                finalTranscript += transcript + ' ';
            } else {
                interimTranscript += transcript;
            }
        }

        // Update UI
        if (interimTranscript) {
            ui.appendTranscript(interimTranscript, false);
        }

        if (finalTranscript) {
            ui.appendTranscript(finalTranscript, true);

            // Trigger callback if set
            if (this.onResultCallback) {
                this.onResultCallback(finalTranscript);
            }
        }
    }

    onResult(callback) {
        this.onResultCallback = callback;
    }
}

export default new AudioManager();
