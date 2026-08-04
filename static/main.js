document.addEventListener("DOMContentLoaded", function () {
  document.getElementById("repo_url").focus();
});

const form = document.getElementById("lookup-form");
const submitBtn = document.getElementById("submit-btn");
const clearBtn = document.getElementById("clear-btn");
const cancelBtn = document.getElementById("cancel-btn");
const resultDiv = document.getElementById("result");
const loadingMessage = document.getElementById("loading-message");

const HOST_NAMES = {
  "github.com": "GitHub",
  "gitlab.com": "GitLab",
  "bitbucket.org": "Bitbucket",
};

function hostForUrl(url) {
  const value = url.trim();
  if (!value) return "";
  let normalized = value;
  if (normalized.startsWith("git@")) {
    normalized = "https://" + normalized.slice(4).replace(":", "/");
  } else if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(normalized)) {
    normalized = "https://" + normalized;
  }
  try {
    return new URL(normalized).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function updateLoadingMessage() {
  const host = hostForUrl(document.getElementById("repo_url").value);
  const name = HOST_NAMES[host];
  loadingMessage.textContent = name
    ? `Looking up the first commit on ${name}...`
    : "Looking up the first commit on GitHub, GitLab, or Bitbucket...";
}

form.addEventListener("htmx:beforeRequest", function () {
  submitBtn.disabled = true;
  updateLoadingMessage();
});

form.addEventListener("htmx:afterRequest", function () {
  submitBtn.disabled = false;
});

document.body.addEventListener("htmx:afterSwap", function (event) {
  if (event.detail.target === resultDiv && resultDiv.children.length > 0) {
    resultDiv.focus();
  }
});

clearBtn.addEventListener("click", function () {
  resultDiv.innerHTML = "";
  loadingMessage.textContent = "Looking up the first commit on GitHub, GitLab, or Bitbucket...";
  document.getElementById("repo_url").focus();
});

cancelBtn.addEventListener("click", function () {
  htmx.trigger("#lookup-form", "htmx:abort");
});

document.querySelectorAll(".example-link").forEach(function (btn) {
  btn.addEventListener("click", function () {
    const input = document.getElementById("repo_url");
    input.value = btn.dataset.url;
    input.focus();
  });
});

document.body.addEventListener("htmx:sendError", function () {
  resultDiv.innerHTML =
    '<div class="error">Network error. Unable to reach the server.</div>';
});

document.getElementById("repo_url").addEventListener("input", function () {
  if (resultDiv.querySelector(".error")) {
    resultDiv.innerHTML = "";
  }
});
