(function () {
  getWidgetDesignSetting();
  window.lodash = _.noConflict();
}());

let PageBodyGrid = function () {
  this.init = function () {
    let options = {
      width: 12,
      float: true,
      verticalMargin: 4,
      cellHeight: 100,
      acceptWidgets: '.grid-stack-item'
    };
    let widget = $('#page_body');
    widget.gridstack(options);
    this.grid = widget.data('gridstack');
  };

  this.addNewWidget = function (node) {
    this.grid.addWidget($(this.widgetTemplate(node)), node.x, node.y, node.width, node.height);
    return false;
  }.bind(this);

  this.updateMainContent = function (node) {
    let mainContents = $("#main_contents");
    this.grid.update(mainContents, node.x, node.y, node.width, node.height);
  };

  this.loadGrid = function (widgetListItems) {
    let items = GridStackUI.Utils.sort(widgetListItems);
    items.forEach(function (node) {
      if ("Main contents" == node.type) {
        this.updateMainContent(node);
        return false;
      }
    }, this);

    items.forEach(function (node) {
      if ("Main contents" != node.type) {
        this.addNewWidget(node);
      }
    }, this);
    return false;
  }.bind(this);

  this.widgetTemplate = function (node) {

    console.log('================NODE==============', node);
    let labelColor = node.label_color;
    let frameBorderColor = ((node.frame_border) ? node.frame_border_color : "");
    let backgroundColor = node.background_color;
    let description = "";
    let leftStyle = 0;
    let paddingHeading = "";
    if (node.type == "Free description" || node.type == "Notice") {
      description = node.description;
      leftStyle = "initial";
      paddingHeading = "inherit";
    }

    let template1 =
    '<div class="grid-stack-item">' +
    '	<div class="container-fluid" style="background-color: ' + backgroundColor + '; border-color: ' + frameBorderColor + ';">' +
    '		<div class="row">' +
    '				<div class="panel panel-default">' +
    '					<div class="panel-heading widget-header" style="color: ' + labelColor + ';">' +
    '						<strong>' + node.name + '</strong>' +
    '					</div>' +
    '				</div>' +
    '		</div>' +
    '		<div class="row">' +
    '				<div class="panel panel-default">' +
    '					<div class="panel-body ql-editor">' + description + '</div>' +
    '				</div>' +
    '		</div>' +
    '	</div>' +
    '</div>';

    let template =
      '<div class="grid-stack-item">' +
      ' <div class="grid-stack-item-content panel panel-default widget" style="background-color: ' + backgroundColor + '; border-color: ' + frameBorderColor + ';">' +
      '     <div class="panel-heading widget-header" style="color: ' + labelColor + ';position: inherit;width: 100%;top: 0;right: inherit; left: ' + leftStyle + ';">' +
      '       <strong>' + node.name + '</strong>' +
      '     </div>' +
      '     <div class="panel-body ql-editor">' + description + '</div>' +
      ' </div>' +
      '</div>';

    return template1;
  };

};

function getWidgetDesignSetting() {
  let community_id = $("#community-id").text();
  if (!community_id) {
    community_id = 'Root Index';
  }
  let url = '/api/admin/load_widget_design_setting/' + community_id;
  $.ajax({
    type: 'GET',
    url: url,
    success: function (data) {
      if (data.error) {
        alert(error);
        $("div#page_body").each(function() {
          $(this).css("display", "block");
        });
        return;
      } else {
        let widgetList = data['widget-settings'];
        if (Array.isArray(widgetList) && widgetList.length) {
          $("#main_contents").addClass("grid-stack-item");
          let pageBodyGrid = new PageBodyGrid();
          pageBodyGrid.init();
          pageBodyGrid.loadGrid(widgetList);
        }
      }
      $("div#page_body").each(function() {
        $(this).css("display", "block");
      });
    }
  });
}
