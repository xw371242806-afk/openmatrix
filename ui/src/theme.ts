import type { ThemeConfig } from 'antd'

/**
 * Research Desk — the single Ant Design theme for OpenMatrix.
 * Warm paper surfaces, hairline borders, ink primary actions and a
 * copper link accent. Signal colors are dark enough for text use so
 * status remains readable when carried by words, not just color.
 */
export const researchDeskTheme: ThemeConfig = {
  token: {
    colorPrimary: '#23262C',
    colorInfo: '#33608A',
    colorLink: '#84491F',
    colorSuccess: '#2E7D4F',
    colorWarning: '#96660A',
    colorError: '#B3372B',
    colorBgLayout: '#F7F7F5',
    colorBgContainer: '#FFFFFF',
    colorBgElevated: '#FFFFFF',
    colorBorder: '#DBDBD7',
    colorBorderSecondary: '#E9E9E6',
    colorText: '#1C1E22',
    colorTextSecondary: '#52565D',
    colorTextTertiary: '#6E727A',
    colorTextQuaternary: '#A0A4AC',
    borderRadius: 8,
    controlHeight: 34,
    boxShadowTertiary: '0 1px 2px rgba(28, 30, 34, 0.05)',
    fontFamily:
      "'Inter', 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif",
    fontSize: 14,
  },
  components: {
    Table: {
      headerBg: '#F5F5F2',
      headerColor: '#52565D',
      rowHoverBg: '#F7F7F4',
      cellPaddingBlock: 12,
      borderColor: '#E9E9E6',
    },
    Card: {
      colorBorderSecondary: '#E9E9E6',
    },
    Drawer: {
      colorBgElevated: '#FFFFFF',
    },
    Modal: {
      colorBgElevated: '#FFFFFF',
    },
    Tag: {
      borderRadiusSM: 6,
    },
    Statistic: {
      titleFontSize: 13,
      contentFontSize: 26,
    },
    Tabs: {
      titleFontSize: 14,
    },
    Segmented: {
      itemSelectedBg: '#23262C',
      itemSelectedColor: '#FFFFFF',
      borderRadius: 999,
    },
    Tooltip: {
      colorBgSpotlight: '#23262C',
    },
  },
}
