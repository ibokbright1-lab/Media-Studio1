document.addEventListener("DOMContentLoaded", () => {
    
    // =====================================================================
    // 1. SPA ROUTING & BREADCRUMBS
    // =====================================================================
    const navLinks = document.querySelectorAll(".nav-link");
    const sections = document.querySelectorAll(".app-section");
    const breadcrumb = document.getElementById("breadcrumb-current");

    navLinks.forEach(link => {
        link.addEventListener("click", (e) => {
            const targetBtn = e.currentTarget;
            
            navLinks.forEach(l => l.classList.remove("active"));
            sections.forEach(s => s.classList.add("hidden"));

            targetBtn.classList.add("active");
            const targetId = targetBtn.getAttribute("data-target");
            document.getElementById(targetId).classList.remove("hidden");
            
            breadcrumb.textContent = targetBtn.querySelector('.nav-text').textContent;
        });
    });

    // =====================================================================
    // 2. MEDIA EXTRACTION (VIDEO & AUDIO)
    // =====================================================================
    const btnAnalyzeVideo = document.getElementById("btn-analyze-video");
    const btnAnalyzeAudio = document.getElementById("btn-analyze-audio");

    async function analyzeMedia(url, type) {
        if (!url) return alert("Please enter a valid URL.");

        const btn = type === 'video' ? btnAnalyzeVideo : btnAnalyzeAudio;
        const originalText = btn.querySelector('span:first-child').textContent;
        
        btn.querySelector('span:first-child').textContent = "Analyzing...";
        btn.disabled = true;

        try {
            const formData = new FormData();
            formData.append("url", url);

            const response = await fetch("/api/info", {
                method: "POST",
                body: formData
            });

            const data = await response.json();
            if (data.error) throw new Error(data.error);

            document.getElementById(`${type}-thumb`).src = data.thumbnail || '';
            document.getElementById(`${type}-title`).textContent = data.title || 'Unknown Title';

            const tbody = document.getElementById(`${type}-format-list`);
            tbody.innerHTML = ""; 

            if (type === 'video') {
                data.videos.forEach(fmt => {
                    const row = `<tr>
                        <td><strong>${fmt.height}p</strong> <span style="color: var(--muted); font-size: 8px;">(${fmt.ext.toUpperCase()})</span></td>
                        <td>${fmt.fps || 'N/A'}</td>
                        <td>${fmt.size_mb ? fmt.size_mb + ' MB' : '--'}</td>
                        <td><button class="btn-download" data-url="${url}" data-kind="video" data-id="${fmt.format_id}">Download</button></td>
                    </tr>`;
                    tbody.insertAdjacentHTML('beforeend', row);
                });
            } else if (type === 'audio') {
                data.audios.forEach(fmt => {
                    const row = `<tr>
                        <td><strong>${fmt.bitrate ? Math.round(fmt.bitrate) + ' kbps' : 'Standard'}</strong></td>
                        <td>MP3 (Converted)</td>
                        <td>${fmt.size_mb ? fmt.size_mb + ' MB' : '--'}</td>
                        <td><button class="btn-download" data-url="${url}" data-kind="audio" data-id="${fmt.format_id}">Download</button></td>
                    </tr>`;
                    tbody.insertAdjacentHTML('beforeend', row);
                });
            }

            document.getElementById(`${type}-results`).classList.remove("hidden");
            
        } catch (error) {
            alert("Analysis Failed: " + error.message);
        } finally {
            btn.querySelector('span:first-child').textContent = originalText;
            btn.disabled = false;
        }
    }

    if (btnAnalyzeVideo) btnAnalyzeVideo.addEventListener("click", () => analyzeMedia(document.getElementById("video-url").value, 'video'));
    if (btnAnalyzeAudio) btnAnalyzeAudio.addEventListener("click", () => analyzeMedia(document.getElementById("audio-url").value, 'audio'));


    // =====================================================================
    // 3. DOWNLOAD TRIGGER & GLOBAL PROGRESS POLling
    // =====================================================================
    document.addEventListener("click", async (e) => {
        if (e.target.classList.contains("btn-download")) {
            const btn = e.target;
            const url = btn.getAttribute("data-url");
            const kind = btn.getAttribute("data-kind");
            const formatId = btn.getAttribute("data-id");

            document.querySelectorAll(".btn-download").forEach(b => b.disabled = true);
            btn.textContent = "Starting...";

            try {
                const formData = new FormData();
                formData.append("url", url);
                formData.append("kind", kind);
                formData.append("format_id", formatId);
                formData.append("fmt", "mp3");

                const response = await fetch("/api/download", {
                    method: "POST",
                    body: formData
                });

                const data = await response.json();
                if (data.error) throw new Error(data.error);

                pollJobProgress(data.job_id);
            } catch (error) {
                alert("Download Error: " + error.message);
                document.querySelectorAll(".btn-download").forEach(b => {
                    b.disabled = false;
                    b.textContent = "Download";
                });
            }
        }
    });

    // =====================================================================
    // 4. THE STUDIO (TRIMMER FROM URL)
    // =====================================================================
    const btnTrim = document.querySelector(".studio-button");

    if (btnTrim) {
        btnTrim.addEventListener("click", async (e) => {
            const url = document.getElementById("trim-url").value;
            const start = document.getElementById("trim-start").value;
            const end = document.getElementById("trim-end").value;
            const btnTextSpan = e.currentTarget.querySelector('span:first-child');

            if (!url) return alert("Please provide a media URL to trim.");
            if (!start || !end) return alert("Please provide both Start and End times.");

            e.currentTarget.disabled = true;
            btnTextSpan.textContent = "Starting...";

            try {
                const formData = new FormData();
                formData.append("url", url);
                formData.append("kind", "video");
                formData.append("start", start);
                formData.append("end", end);

                const response = await fetch("/api/download", {
                    method: "POST",
                    body: formData
                });
                const data = await response.json();
                if (data.error) throw new Error(data.error);

                pollJobProgress(data.job_id);

            } catch (error) {
                alert("Trimming Failed: " + error.message);
            } finally {
                e.currentTarget.disabled = false;
                btnTextSpan.textContent = "Cut & Download";
            }
        });
    }

        // =====================================================================
    // 5. RESILIENT PROGRESS POLLER ENGINE
    // =====================================================================
    function pollJobProgress(jobId) {
        const tray = document.getElementById("progress-tray");
        const statusText = document.getElementById("progress-status");
        const percentText = document.getElementById("progress-percent");
        const barFill = document.getElementById("progress-bar-fill");

        if (!tray) return;

        tray.classList.remove("hidden");
        barFill.style.background = "linear-gradient(90deg, #4f8cff, #8b5cf6)"; 
        barFill.style.width = "0%";

        let consecutiveFailures = 0;
        const MAX_FAILURES = 4; // Tolerate up to 4 consecutive network blips

        const interval = setInterval(async () => {
            try {
                const res = await fetch(`/api/jobs/${jobId}`);
                
                // If server is momentarily busy or returns empty response, don't crash
                if (!res.ok) {
                    throw new Error(`HTTP error ${res.status}`);
                }

                const text = await res.text();
                if (!text || !text.trim()) {
                    throw new Error("Empty response received");
                }

                const data = JSON.parse(text);
                consecutiveFailures = 0; // Reset counter on successful poll

                if (data.error) throw new Error(data.error);

                const progress = Math.round(data.progress || 0);
                percentText.textContent = `${progress}%`;
                barFill.style.width = `${progress}%`;
                statusText.textContent = data.message || "Processing...";

                if (data.status === "done") {
                    clearInterval(interval);
                    statusText.textContent = "Complete!";
                    barFill.style.background = "var(--success)"; 
                    
                    const finalUrl = data.url || (data.result && data.result.url);
                    
                    if (!finalUrl) {
                        alert("Finished, but no download link found in response.");
                    } else {
                        window.location.href = finalUrl;
                    }

                    setTimeout(() => {
                        tray.classList.add("hidden");
                        document.querySelectorAll(".btn-download").forEach(b => {
                            b.disabled = false;
                            b.textContent = "Download";
                        });
                    }, 4000);
                } 
                else if (data.status === "error") {
                    throw new Error(data.error || "Processing failed.");
                }
            } catch (error) {
                consecutiveFailures++;
                console.warn(`Polling attempt failed (${consecutiveFailures}/${MAX_FAILURES}):`, error.message);

                // Only fail completely if 4 requests fail in a row
                if (consecutiveFailures >= MAX_FAILURES) {
                    clearInterval(interval);
                    statusText.textContent = "Error";
                    barFill.style.background = "#ef4444"; 
                    alert("Connection interrupted: " + error.message);
                    
                    document.querySelectorAll(".btn-download").forEach(b => {
                        b.disabled = false;
                        b.textContent = "Download";
                    });
                    
                    setTimeout(() => tray.classList.add("hidden"), 3000);
                }
            }
        }, 1000);
    }

        // =====================================================================
    // 6. THE STUDIO (UPLOAD LOCAL FILE WITH ON-SCREEN PREVIEW)
    // =====================================================================
    const localMediaUpload = document.getElementById("local-media-upload");

    if (localMediaUpload) {
        localMediaUpload.addEventListener("change", async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            // 1. CREATE THE ON-SCREEN VIDEO PREVIEW
            // This grabs the dropzone box and injects a video player right into it
            const dropzone = document.querySelector(".studio-dropzone");
            if (dropzone) {
                // Generate a temporary local URL so the browser can play the file
                const fileUrl = URL.createObjectURL(file);
                
                // Replace the "Browse Files" text with a functional video player
                dropzone.innerHTML = `
                    <p style="margin-bottom: 10px; color: var(--success); font-weight: bold;">File Selected: ${file.name}</p>
                    <video controls style="max-width: 100%; border-radius: 8px; border: 1px solid var(--border);">
                        <source src="${fileUrl}" type="${file.type}">
                        Your browser does not support the video tag.
                    </video>
                    <p style="margin-top: 10px; font-size: 0.8rem; color: var(--muted);">Watch the preview above to find your exact Start and End times.</p>
                `;
            }

            // 2. PREPARE THE TIMESTAMPS FOR UPLOAD
            const start = document.getElementById("trim-start") ? document.getElementById("trim-start").value : "";
            const end = document.getElementById("trim-end") ? document.getElementById("trim-end").value : "";

            const formData = new FormData();
            formData.append("file", file);
            if (start) formData.append("start", start);
            if (end) formData.append("end", end);
            
            // 3. TRIGGER THE PROGRESS BAR
            const tray = document.getElementById("progress-tray");
            const statusText = document.getElementById("progress-status");
            const percentText = document.getElementById("progress-percent");
            const barFill = document.getElementById("progress-bar-fill");

            if (tray) {
                tray.classList.remove("hidden");
                statusText.textContent = `Uploading ${file.name}...`;
                percentText.textContent = "Uploading...";
                barFill.style.background = "linear-gradient(90deg, #4f8cff, #8b5cf6)";
                barFill.style.width = "30%";
            }

            // 4. SEND TO BACKEND
            try {
                const response = await fetch("/api/trim", {
                    method: "POST",
                    body: formData
                });

                const data = await response.json();
                if (data.error) throw new Error(data.error);

                if (data.job_id) {
                    pollJobProgress(data.job_id);
                }

            } catch (error) {
                alert("Upload Error: " + error.message);
                if (tray) tray.classList.add("hidden");
            } finally {
                e.target.value = ""; 
            }
        });
    }
