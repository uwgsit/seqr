import React from 'react'
import { shallow, configure } from 'enzyme'
import Adapter from 'enzyme-adapter-react-16'
import configureStore from 'redux-mock-store'
import Footer from './Footer'

configure({ adapter: new Adapter() })

test('shallow-render without crashing', () => {
  const store = configureStore()({ meta: {version: '0.1' } })

  shallow(<Footer store={store} />)
})
