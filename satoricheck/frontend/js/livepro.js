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
        this.totalSeconds = 0;
        this.onTranscriptCallback = null;
        this.onStopCallback = null;

        // Config from backend
        this.cpPerMinute = 1;
        this.available = false;

        // Connection Stability
        this.maxRetries = 3;
        this.retryCount = 0;
        this.isRetrying = false;
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
                // Live Pro not available (Deepgram not configured)
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
     * Set callback for when session stops
     */
    onStop(callback) {
        this.onStopCallback = callback;
    }

    /**
     * Start Live Pro session
     */
    async start(deviceId = null, language = 'en') {
        if (this.isActive) {
            console.warn('Live Pro session already active');
            return false;
        }

        this.currentDeviceId = deviceId;
        this.currentLanguage = language;

        // Explicitly request microphone permission first to match standard mode UX
        // This ensures the browser prompt appears before we create a backend session
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.getTracks().forEach(track => track.stop()); // Release immediately, just checking permission
        } catch (permError) {
            console.error('Microphone permission denied:', permError);
            ui.showToast('Microphone access is required for Live Pro', 'error');
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
            // Get audio stream with strict constraints for Deepgram compatibility
            // Force 16kHz Mono to avoid Opux/WebM complexity in the proxy
            const constraints = {
                audio: {
                    deviceId: deviceId ? { exact: deviceId } : undefined,
                    channelCount: 1,
                    sampleRate: 16000,
                    echoCancellation: false,
                    noiseSuppression: false, // Let Deepgram handle this
                    autoGainControl: false
                }
            };

            // Security Check: Ensure secure context (HTTPS)
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                throw new Error('Microphone access requires a secure connection (HTTPS).');
            }

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
                resolve();
            };

            this.webSocket.onmessage = (event) => {
                this.handleTranscript(event.data);
            };

            this.webSocket.onerror = (error) => {
                console.error('WebSocket error:', error);
                reject(new Error('WebSocket connection failed'));
            };

            this.webSocket.onclose = async (event) => {
                console.log('Deepgram WebSocket closed:', event.code, event.reason);

                if (this.isActive && !this.isRetrying) {
                    // Check if we should retry (Code 1006 is abnormal closure)
                    if (this.retryCount < this.maxRetries) {
                        console.log(`Connection lost. Retrying (${this.retryCount + 1}/${this.maxRetries})...`);
                        this.isRetrying = true;
                        this.retryCount++;

                        try {
                            // Wait 1s before retry
                            await new Promise(r => setTimeout(r, 1000));

                            // Re-establish WebSocket only (keep media stream)
                            // Note: We might need a new session ID if the backend closed it, 
                            // but for network blips, re-connecting to the same proxy URL might work 
                            // if the backend logic supports it. 
                            // However, the safest backend approach implies a new session.
                            // For this iteration, we'll try to restart the full flow to be safe.

                            this.isRetrying = false; // Reset flag before restart
                            this.stop(); // Clean up current state
                            await this.start(this.currentDeviceId, this.currentLanguage); // Restart

                        } catch (retryError) {
                            console.error('Retry failed:', retryError);
                            this.stop();
                            ui.showToast('Connection failed after retries', 'error');
                        }
                    } else {
                        // Max retries reached
                        this.stop();
                        ui.showToast('Live Pro connection lost', 'warning');
                    }
                }
            };

            // Timeout
            setTimeout(() => {
                if (this.webSocket && (this.webSocket.readyState !== WebSocket.OPEN)) {
                    // Only reject if we are still trying to connect (and haven't been cleaned up)
                    // If this.webSocket is null, cleanup() was probably called, so we ignore
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
            } catch (error) {
                console.error('End session failed:', error);
            }
        }

        this.cleanup();

        // Notify listeners
        if (this.onStopCallback) {
            this.onStopCallback();
        }

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
