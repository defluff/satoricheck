/**
 * Audio UI Module
 * Handles microphone selection
 */

class AudioUI {
    constructor() {
        this.micSelect = document.getElementById('mic-select');
        this.selectedMicId = localStorage.getItem('selectedMicId') || '';

        if (this.micSelect) {
            this.micSelect.value = this.selectedMicId;
        }
    }

    async updateAudioDevices() {
        if (!this.micSelect) return;

        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const audioInputs = devices.filter(device => device.kind === 'audioinput');

            // Clear except default
            this.micSelect.innerHTML = '<option value="">Default System Microphone</option>';

            audioInputs.forEach(device => {
                const option = document.createElement('option');
                option.value = device.deviceId;
                // If label is empty, browser is blocking labels (need permission)
                option.textContent = device.label || `Microphone ${this.micSelect.length}`;
                if (device.deviceId === this.selectedMicId) {
                    option.selected = true;
                }
                this.micSelect.appendChild(option);
            });
        } catch (error) {
            console.error('Error listing audio devices:', error);
        }
    }
}

export default new AudioUI();
