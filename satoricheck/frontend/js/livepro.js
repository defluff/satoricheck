/**
 * Live Pro Audio Module
 * Handles Deepgram WebSocket streaming for premium transcription
 */

import api from './api.js';
import ui from './ui.js';

class LiveProManager {
    constructor() {
        this.isActive = false;
        this.webSocket = null;
        this.mediaRecorder = null;
        this.audioStream = null;
        this.sessionStartTime = null;
        this.sessionId = null;
        this.heartbeatInterval = null;
        this.totalSeconds = 0;
        this.onTranscriptCallback = null;

        // Config from backend
        this.cpPerMinute = 1;
        this.available = false;
    }

    /**
     * Initialize Live Pro - check availability
     */
    async init() {
        try {
            const config = await api.getLiveProConfig();
            this.available = config.available;
            this.cpPerMinute = config.cp_per_minute || 1;

            if (!this.available) {
                console.log('Live Pro not available (Deepgram not configured)');
            }

            // Add beforeunload handler to cleanup session if user closes tab
            window.addEventListener('beforeunload', (event) => {
                if (this.isActive && this.sessionId) {
                    // Use sendBeacon to ensure request completes even as page unloads
                    const data = JSON.stringify({ session_id: this.sessionId });
                    const blob = new Blob([data], { type: 'application/json' });
                    navigator.sendBeacon('/api/live-pro/end', blob);
                    this.cleanup();
                }
            });

            return this.available;
        } catch (error) {
            console.error('Failed to init Live Pro:', error);
            this.available = false;
            return false;
        }
    }

    /**
     * Check if Live Pro is available
     */
    isAvailable() {
        return this.available;
    }

    /**
     * Set callback for transcript results
     */
    onTranscript(callback) {
        this.onTranscriptCallback = callback;
    }

    /**
     * Start Live Pro session
     */
    async start(deviceId = null, language = 'en') {
        if (this.isActive) {
            console.warn('Live Pro session already active');
            return false;
        }

        try {
            // Get session config from backend
            const session = await api.startLiveProSession(language, deviceId);

            if (!session.success) {
                throw new Error(session.error || 'Failed to start session');
            }

            // Store session ID
            this.sessionId = session.session_id;

            // Get audio stream
            const constraints = {
                audio: deviceId ? { deviceId: { exact: deviceId } } : true
            };

            this.audioStream = await navigator.mediaDevices.getUserMedia(constraints);

            // Connect to backend WebSocket proxy (auth handled server-side)
            await this.connectWebSocket(session.websocket_url);

            // Start recording and streaming
            this.startStreaming();

            // Start heartbeat (10s interval)
            this.startHeartbeat();

            this.isActive = true;
            this.sessionStartTime = Date.now();
            this.totalSeconds = 0;

            ui.showToast('⚡ Live Pro active', 'success');

            return true;

        } catch (error) {
            console.error('Failed to start Live Pro:', error);
            this.cleanup();

            if (error.message.includes('Insufficient')) {
                ui.showToast('Not enough CP for Live Pro', 'error');
                ui.showModal('buy-tokens-modal');
            } else {
                ui.showToast('Failed to start Live Pro: ' + error.message, 'error');
            }

            return false;
        }
    }

    /**
     * Connect to WebSocket proxy (no auth header needed - session validated server-side)
     */
    connectWebSocket(url) {
        return new Promise((resolve, reject) => {
            // Connect directly to our backend proxy - no auth header needed
            // The proxy validates the session server-side
            this.webSocket = new WebSocket(url);

            this.webSocket.onopen = () => {
                console.log('WebSocket proxy connected');
                resolve();
            };

            this.webSocket.onmessage = (event) => {
                this.handleTranscript(event.data);
            };

            this.webSocket.onerror = (error) => {
                console.error('WebSocket error:', error);
                reject(new Error('WebSocket connection failed'));
            };

            this.webSocket.onclose = (event) => {
                console.log('WebSocket closed:', event.code, event.reason);
                if (this.isActive) {
                    // Unexpected close - try to reconnect or stop
                    this.stop();
                    ui.showToast('Live Pro connection lost', 'warning');
                }
            };

            // Timeout
            setTimeout(() => {
                if (this.webSocket.readyState !== WebSocket.OPEN) {
                    reject(new Error('WebSocket connection timeout'));
                }
            }, 10000);
        });
    }

    /**
     * Handle transcript from Deepgram
     */
    handleTranscript(data) {
        try {
            const response = JSON.parse(data);

            // Check for transcript
            if (response.channel && response.channel.alternatives) {
                const transcript = response.channel.alternatives[0].transcript;
                const isFinal = response.is_final;

                if (transcript && this.onTranscriptCallback) {
                    this.onTranscriptCallback(transcript, isFinal);
                }
            }
        } catch (error) {
            console.error('Error parsing Deepgram response:', error);
        }
    }

    /**
     * Start streaming audio to Deepgram
     */
    startStreaming() {
        // Use MediaRecorder to capture audio
        const options = { mimeType: 'audio/webm;codecs=opus' };

        // Fallback for browsers that don't support webm
        if (!MediaRecorder.isTypeSupported(options.mimeType)) {
            options.mimeType = 'audio/webm';
        }

        this.mediaRecorder = new MediaRecorder(this.audioStream, options);

        this.mediaRecorder.ondataavailable = async (event) => {
            if (event.data.size > 0 && this.webSocket?.readyState === WebSocket.OPEN) {
                // Convert to array buffer and send
                const buffer = await event.data.arrayBuffer();
                this.webSocket.send(buffer);
            }
        };

        // Send data every 100ms for real-time streaming
        this.mediaRecorder.start(100);
    }

    /**
     * Start heartbeat (send every 10 seconds)
     * Server handles billing automatically every 30 seconds
     */
    startHeartbeat() {
        this.heartbeatInterval = setInterval(async () => {
            if (!this.sessionId) return;

            try {
                const result = await api.liveProHeartbeat(this.sessionId);

                if (result.status === 'ok') {
                    // Update balance if CP was deducted
                    if (result.new_balance !== undefined) {
                        ui.updateBalance(result.new_balance);
                        const elapsed = Math.floor((Date.now() - this.sessionStartTime) / 1000);
                        this.totalSeconds = elapsed;
                        console.log(`Live Pro heartbeat: ${result.cp_deducted} CP deducted`);
                    }
                } else if (result.status === 'insufficient_balance') {
                    // Out of credits
                    ui.showToast('Out of CP! Stopping Live Pro...', 'warning');
                    await this.stop();
                }
            } catch (error) {
                console.error('Heartbeat failed:', error);
                // Don't stop on heartbeat failure - server will clean up abandoned sessions
            }
        }, 10000); // Heartbeat every 10 seconds
    }

    /**
     * Stop Live Pro session
     */
    async stop() {
        if (!this.isActive) return;

        this.isActive = false;

        // Calculate final time
        const finalSeconds = Math.floor((Date.now() - this.sessionStartTime) / 1000);

        // End session on backend (server handles final billing)
        if (this.sessionId) {
            try {
                const result = await api.endLiveProSession(this.sessionId);
                console.log(`Live Pro session ended: ${result.cp_consumed} CP consumed`);
            } catch (error) {
                console.error('End session failed:', error);
            }
        }

        this.cleanup();

        ui.showToast(`Live Pro stopped (${Math.floor(finalSeconds / 60)}m ${finalSeconds % 60}s)`, 'info');
    }

    /**
     * Cleanup resources
     */
    cleanup() {
        // Stop heartbeat
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }

        this.sessionId = null;

        // Stop media recorder
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
        }
        this.mediaRecorder = null;

        // Close WebSocket
        if (this.webSocket) {
            this.webSocket.close();
            this.webSocket = null;
        }

        // Stop audio stream
        if (this.audioStream) {
            this.audioStream.getTracks().forEach(track => track.stop());
            this.audioStream = null;
        }

        this.isActive = false;
    }

    /**
     * Toggle Live Pro on/off
     */
    async toggle(deviceId = null, language = 'en') {
        if (this.isActive) {
            await this.stop();
            return false;
        } else {
            return await this.start(deviceId, language);
        }
    }
}

export default new LiveProManager();
