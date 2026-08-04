document.body.addEventListener("htmx:responseError", function (event) {
  const resultDiv = document.getElementById("result");
  const status = event.detail.xhr.status;
  let message = "An unexpected error occurred. Please try again.";

  if (status === 400) {
    message =
      "Invalid repository URL or git error. Please check the URL and try again.";
  } else if (status === 403) {
    message =
      "Repository host not allowed. Only GitHub, GitLab, and Bitbucket are supported.";
  } else if (status === 504) {
    message =
      "The operation timed out. The repository might be too large or unavailable.";
  } else if (status === 0) {
    message = "Network error. Please check your internet connection.";
  }

  resultDiv.innerHTML = `<div class="error">${message}</div>`;
});

document.body.addEventListener("htmx:sendError", function (event) {
  const resultDiv = document.getElementById("result");
  resultDiv.innerHTML =
    '<div class="error">Network error. Unable to reach the server.</div>';
});

document.addEventListener("DOMContentLoaded", function () {
  document.getElementById("repo_url").focus();
});

const form = document.getElementById("lookup-form");
const submitBtn = document.getElementById("submit-btn");
const clearBtn = document.getElementById("clear-btn");
const cancelBtn = document.getElementById("cancel-btn");

form.addEventListener("htmx:beforeRequest", function () {
  submitBtn.disabled = true;
});

form.addEventListener("htmx:afterRequest", function () {
  submitBtn.disabled = false;
});

clearBtn.addEventListener("click", function () {
  document.getElementById("result").innerHTML = "";
  document.getElementById("repo_url").focus();
});

cancelBtn.addEventListener("click", function () {
  htmx.trigger("#lookup-form", "htmx:abort");
});

document.getElementById("repo_url").addEventListener("input", function () {
  const resultDiv = document.getElementById("result");
  if (resultDiv.querySelector(".error")) {
    resultDiv.innerHTML = "";
  }
});
