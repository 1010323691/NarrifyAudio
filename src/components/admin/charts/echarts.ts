// Tree-shaken ECharts registration: only the chart types the admin console uses.
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, HeatmapChart, LineChart, PieChart } from 'echarts/charts'
import {
  DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent, VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart, BarChart, PieChart, GaugeChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, VisualMapComponent, MarkLineComponent,
  CanvasRenderer,
])

export { echarts }
export type { EChartsCoreOption } from 'echarts/core'
