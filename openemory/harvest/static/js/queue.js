/* ajax functionality for ingesting items in the harvest queue */
$(document).ready(function () {

    // NOTE: There is some overlap in ingest/ignore functionality
    // below (particularly success/error handling and message
    // display), but it is not obvious how to combine them.

    // configure ingest buttons to POST pmcid when clicked
    $(".content").on("click", ".ingest", function() {
        var data = { pmcid: $(this).find(".pmcid").html()};
        var entry = $(this).parent();
        var msg = entry.find('.message');
        var errclass = 'error-msg';
        var success_class = 'success-msg';
        var csrftoken = $("#csrftoken input").attr("value");

        if ( ! entry.hasClass('working') ) {
          entry.addClass('working');
          $.ajax({
              type: 'POST',
              data: data,
              url: $(this).attr('href'),
              headers: {'X-CSRFTOKEN': csrftoken},
              success: function(data, status, xhr) {
                  msg.html(data || 'Success');
                  msg.removeClass(errclass).addClass(success_class).show().delay(1500).fadeOut();
                  // change queue item class on success so display can be updated
                  entry.removeClass('working').addClass('ingested');
                  var location = xhr.getResponseHeader('Location');
                  // display view and edit links based on returned location
                  var view_link = $('<a>view</a>').attr('href', location).addClass('link');
                  var edit_link = $('<a>review</a>').attr('href', location + 'edit/').addClass('link');
                  entry.prepend(edit_link).prepend(view_link);
              },
              error: function(data, status, xhr) {
                  msg.html('Error: ' + data.responseText);
                  msg.removeClass(success_class).addClass(errclass).show();
                  // NOTE: not fading error message out, since it may
                  // need to be reported
              }

          });
        }
        return false;
    });

    // configure ignore buttons to DELETE specified url when clicked
    $(".content").on("click", ".ignore", function() {
        var entry = $(this).parent();
        var msg = entry.find('.message');
        var errclass = 'error-msg';
        var success_class = 'success-msg';
        var csrftoken = $("#csrftoken input").attr("value");

        if ( ! entry.hasClass('working') ) {
          entry.addClass('working');
          $.ajax({
              type: 'DELETE',
              url: $(this).attr('href'),
              headers: {'X-CSRFTOKEN': csrftoken},
              success: function(data, status, xhr) {
                  msg.html(data || 'Success');
                  msg.removeClass(errclass).addClass(success_class).show().delay(1500).fadeOut();
                  // change queue item on success so display can be updated
                  entry.removeClass('working').addClass('ignored');
              },
              error: function(data, status, xhr) {
                  msg.html('Error: ' + data.responseText);
                  msg.removeClass(success_class).addClass(errclass).show();
              }

          });
        }
        return false;
    });

});


