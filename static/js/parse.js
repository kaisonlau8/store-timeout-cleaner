document.addEventListener('DOMContentLoaded', () => {
    const allUpload = document.getElementById('all-upload');
    const filterUpload = document.getElementById('filter-upload');
    const allFileInput = document.getElementById('all-file');
    const filterFileInput = document.getElementById('filter-file');
    const allFilename = document.getElementById('all-filename');
    const filterFilename = document.getElementById('filter-filename');
    const analyzeBtn = document.getElementById('analyze-btn');
    const errorMsg = document.getElementById('error-msg');
    const successMsg = document.getElementById('success-msg');

    let allFile = null;
    let filterFile = null;

    function setupUpload(box, input, filenameEl, type) {
        box.addEventListener('click', () => input.click());

        box.addEventListener('dragover', (e) => {
            e.preventDefault();
            box.classList.add('dragover');
        });

        box.addEventListener('dragleave', () => {
            box.classList.remove('dragover');
        });

        box.addEventListener('drop', (e) => {
            e.preventDefault();
            box.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0], type);
            }
        });

        input.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFile(e.target.files[0], type);
            }
        });
    }

    function handleFile(file, type) {
        if (type === 'all') {
            allFile = file;
            allFilename.textContent = file.name;
            document.getElementById('all-upload').classList.add('has-file');
        } else {
            filterFile = file;
            filterFilename.textContent = file.name;
            document.getElementById('filter-upload').classList.add('has-file');
        }
        checkReady();
    }

    function checkReady() {
        if (allFile && filterFile) {
            analyzeBtn.disabled = false;
        }
    }

    setupUpload(allUpload, allFileInput, allFilename, 'all');
    setupUpload(filterUpload, filterFileInput, filterFilename, 'filter');

    analyzeBtn.addEventListener('click', async () => {
        if (!allFile || !filterFile) return;

        analyzeBtn.disabled = true;
        analyzeBtn.querySelector('.btn-text').hidden = true;
        analyzeBtn.querySelector('.btn-loading').hidden = false;
        errorMsg.hidden = true;
        successMsg.hidden = true;

        const formData = new FormData();
        formData.append('all_file', allFile);
        formData.append('filter_file', filterFile);

        try {
            const res = await fetch('/api/upload', {
                method: 'POST',
                body: formData,
            });
            const data = await res.json();

            if (!data.success) {
                throw new Error(data.error || '解析失败');
            }

            successMsg.hidden = false;
        } catch (err) {
            errorMsg.textContent = '错误: ' + err.message;
            errorMsg.hidden = false;
        } finally {
            analyzeBtn.disabled = false;
            analyzeBtn.querySelector('.btn-text').hidden = false;
            analyzeBtn.querySelector('.btn-loading').hidden = true;
        }
    });
});
