/*
 * Progressive enhancement for the admin panel.
 *
 * Everything here is optional: each form works without JavaScript, this only
 * removes friction. No inline handlers anywhere, so the Content-Security-Policy
 * can stay at script-src 'self'.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------
   * Record add/edit modal.
   *
   * One modal serves both. The button that opened it carries the record in
   * data- attributes; "Add" carries none, so the form resets to blank.
   * ------------------------------------------------------------- */
  var recordModal = document.getElementById("record-modal");
  if (recordModal) {
    recordModal.addEventListener("show.bs.modal", function (event) {
      var trigger = event.relatedTarget;
      if (!trigger) return;

      var field = function (name) {
        return recordModal.querySelector('[data-record-field="' + name + '"]');
      };
      var data = function (name) {
        return trigger.getAttribute("data-record-" + name) || "";
      };

      var isNew = trigger.hasAttribute("data-record-new");
      var title = recordModal.querySelector("[data-record-title]");
      if (title) title.textContent = isNew ? "Add record" : "Edit record";

      field("name").value = isNew ? "" : data("name");
      field("ttl").value = isNew ? field("ttl").defaultValue : data("ttl");
      field("content").value = isNew ? "" : data("content");
      field("comment").value = isNew ? "" : data("comment");
      field("disabled").checked = !isNew && data("disabled") === "1";

      var type = field("type");
      type.value = isNew ? "A" : data("type");
      // An existing record of a type not in the dropdown must still be editable.
      if (!isNew && type.value !== data("type")) {
        var option = document.createElement("option");
        option.value = option.textContent = data("type");
        type.appendChild(option);
        type.value = data("type");
      }

      // Tells the server which set to replace when the name or type changes.
      field("original_name").value = isNew ? "" : data("name");
      field("original_type").value = isNew ? "" : data("type");

      // SOA is edited, never renamed or retyped.
      var isSoa = !isNew && data("type") === "SOA";
      field("name").readOnly = isSoa;
      type.disabled = isSoa;
      if (isSoa) {
        // A disabled select submits nothing; keep the type in the payload.
        field("original_type").value = "SOA";
        var hidden = recordModal.querySelector('input[name="type"][type="hidden"]');
        if (!hidden) {
          hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = "type";
          recordModal.querySelector("form").appendChild(hidden);
        }
        hidden.value = "SOA";
        hidden.disabled = false;
      } else {
        var stale = recordModal.querySelector('input[name="type"][type="hidden"]');
        if (stale) stale.disabled = true;
      }
    });
  }

  /* ---------------------------------------------------------------
   * Delete-record confirmation: carry the name and type across.
   * ------------------------------------------------------------- */
  var deleteModal = document.getElementById("delete-record-modal");
  if (deleteModal) {
    deleteModal.addEventListener("show.bs.modal", function (event) {
      var trigger = event.relatedTarget;
      if (!trigger) return;
      var name = trigger.getAttribute("data-record-name") || "";
      var type = trigger.getAttribute("data-record-type") || "";
      deleteModal.querySelector('[data-delete-field="name"]').value = name;
      deleteModal.querySelector('[data-delete-field="type"]').value = type;
      var label = deleteModal.querySelector("[data-delete-label]");
      if (label) label.textContent = name + " " + type;
    });
  }

  /* ---------------------------------------------------------------
   * Client-side table and list filtering.
   * ------------------------------------------------------------- */
  function wireFilter(input, rows) {
    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      rows.forEach(function (row) {
        var haystack = (row.textContent || "").toLowerCase();
        row.hidden = needle !== "" && haystack.indexOf(needle) === -1;
      });
    });
  }

  document.querySelectorAll("[data-table-filter]").forEach(function (input) {
    var table = document.querySelector(input.getAttribute("data-table-filter"));
    if (!table) return;
    wireFilter(input, Array.prototype.slice.call(table.querySelectorAll("tbody tr")));
  });

  document.querySelectorAll("[data-list-filter]").forEach(function (input) {
    var list = document.querySelector(input.getAttribute("data-list-filter"));
    if (!list) return;
    wireFilter(input, Array.prototype.slice.call(list.querySelectorAll("label")));
  });

  /* ---------------------------------------------------------------
   * Show/hide password.
   * ------------------------------------------------------------- */
  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.getAttribute("data-password-toggle"));
      if (!input) return;
      var hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      button.setAttribute("aria-label", hidden ? "Hide password" : "Show password");
      var icon = button.querySelector("i");
      if (icon) icon.className = hidden ? "ti ti-eye-off" : "ti ti-eye";
    });
  });

  /* ---------------------------------------------------------------
   * Confirmation for destructive forms outside a modal.
   * ------------------------------------------------------------- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  /* ---------------------------------------------------------------
   * New-zone form: show the fields that apply to the selected kind.
   * ------------------------------------------------------------- */
  var kindSelect = document.querySelector("[data-zone-kind]");
  if (kindSelect) {
    var applyKind = function () {
      var kind = kindSelect.value;
      document.querySelectorAll("[data-when-kind]").forEach(function (block) {
        var kinds = block.getAttribute("data-when-kind").split(",");
        block.classList.toggle("d-none", kinds.indexOf(kind) === -1);
      });
    };
    kindSelect.addEventListener("change", applyKind);
    applyKind();
  }
})();
