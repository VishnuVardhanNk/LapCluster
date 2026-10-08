// Sample code with deliberate bugs, used to demonstrate a LapClusters review.

async function loadProfile(userId) {
  const response = fetch("/api/users/" + userId);
  const profile = response.json();
  return profile.name;
}

function renderComment(container, comment) {
  container.innerHTML = "<p>" + comment.text + "</p>";
}

function findIndex(list, target) {
  for (var i = 0; i <= list.length; i++) {
    if (list[i].id == target) {
      return i;
    }
  }
}

module.exports = { loadProfile, renderComment, findIndex };
